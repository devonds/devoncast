// Remote MCP server for claude.ai (web and mobile) plus the job queue the Mac polls.
//
// claude.ai can't reach the MCP server on the Mac, so tools here only queue jobs in D1.
// `devoncast poll` on the Mac claims them over /api/jobs, renders and publishes.

import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

export interface Env {
	DB: D1Database;
	BUCKET: R2Bucket;
	PUBLIC_BASE_URL: string;
	HOSTNAME: string;
	MCP_PATH_SECRET: string; // the MCP endpoint is /<secret>/mcp; the URL is the credential
	WORKER_TOKEN: string; // bearer token the Mac uses for /api/jobs
	FEED_PREFIX: string; // the feed's secret path segment in the R2 bucket
}

// Keep in sync with SCRIPT_GUIDE in src/devoncast/script.py.
const SCRIPT_GUIDE = `Rewrite the document as a script to be read aloud by a text-to-speech engine, for one listener
on a walk or in the car. Rules:

- Keep all the substance, in the original order. This is a faithful audio edition, not a summary.
  Cut only what makes no sense in audio: URLs, file paths, code, figure references, "see above",
  and administrative bits like scheduling logistics, video call links, or contact details.
- Start each section with a line "# <section title>" on its own (a chapter marker; it is not
  spoken), then a short spoken lead-in such as "Section two: modeling approach." so the listener
  hears where they are. Use one chapter per top-level section; fold small subsections into
  their parent and introduce them in speech instead.
- Open with one or two sentences saying what this is and what it covers. End with a brief sign-off.
- Turn bullet lists, tables, and fragments into full spoken sentences. Say "first", "second",
  "and finally" where order matters.
- Write numbers, symbols and abbreviations as they should be spoken: "about three hundred",
  "thirty percent", "versus", "for example". Write acronyms that are said letter by letter in
  plain capitals with no spaces or periods (AI, CRM, ROC AUC, DBT); the speech engine spells
  them out. Never write single letters separated by spaces. Keep words said as words ("SaaS").
  Prefer the expanded form on first use.
- Short sentences. No parentheses, no emoji, no markdown other than the "# " chapter lines.
- Address the listener as "you" if the document does.`;

const VOICES = "af_heart (default), af_bella, af_nicole, am_michael, am_fenrir, bf_emma, bm_george";

const text = (t: string) => ({ content: [{ type: "text" as const, text: t }] });

function createServer(env: Env) {
	return () => {
		const server = new McpServer(
			{ name: "devoncast", version: "1.0.0" },
			{
				instructions:
					"Devoncast turns text into episodes of Devon's private podcast feed. To add something " +
					"you wrote or can see in the conversation (an artifact, document, notes, an article you " +
					"read), write the speech script yourself following the add_episode guidelines and call " +
					"add_episode. To add a web page you have not read, call add_url. Jobs render on Devon's " +
					"Mac, usually within a few minutes; check them with job_status.",
			},
		);

		server.registerTool(
			"add_episode",
			{
				description:
					`Queue an episode from a speech script you have written.\n\nWrite \`script\` following these guidelines:\n\n${SCRIPT_GUIDE}\n\n` +
					"`title`: specific, under 70 characters. `summary`: one or two sentences. `show_notes`: markdown, " +
					`a short paragraph then a bullet per chapter. \`voice\` is optional: ${VOICES}.`,
				inputSchema: z.object({
					title: z.string().max(200),
					script: z.string().min(1),
					summary: z.string(),
					show_notes: z.string().optional(),
					source_url: z.string().url().optional(),
					voice: z.string().optional(),
				}),
			},
			async (args) => {
				const id = await enqueue(env, "script", args);
				const words = args.script.split(/\s+/).length;
				return text(
					`Queued "${args.title}" (${words} words, about ${Math.round(words / 150)} minutes of audio). ` +
						`Job id: ${id}. Devon's Mac picks up jobs within a minute when it is awake.`,
				);
			},
		);

		server.registerTool(
			"add_url",
			{
				description:
					"Queue an episode from a web page. Devon's Mac fetches the article, rewrites it for " +
					`listening and renders it. Use add_episode instead when you already have the text. \`voice\` is optional: ${VOICES}.`,
				inputSchema: z.object({ url: z.string().url(), voice: z.string().optional() }),
			},
			async (args) => text(`Queued ${args.url}. Job id: ${await enqueue(env, "url", args)}.`),
		);

		server.registerTool(
			"job_status",
			{
				description:
					"Status of a job: queued (waiting for the Mac), claimed, accepted (on the Mac, waiting its turn), " +
					"rendering, retrying (an attempt failed; the Mac tries again on its own, error says when), " +
					"published or failed. Use retry_job on a failed job.",
				inputSchema: z.object({ job_id: z.string() }),
				annotations: { readOnlyHint: true },
			},
			async ({ job_id }) => {
				const job = await env.DB.prepare(
					"SELECT id, kind, status, title, episode_id, error, created_at, updated_at FROM jobs WHERE id = ?",
				)
					.bind(job_id)
					.first();
				return text(job ? JSON.stringify(job, null, 2) : `No job ${job_id}`);
			},
		);

		server.registerTool(
			"list_jobs",
			{
				description: "The 10 most recent jobs and their status.",
				inputSchema: z.object({}),
				annotations: { readOnlyHint: true },
			},
			async () => {
				const { results } = await env.DB.prepare(
					"SELECT id, kind, status, title, error, created_at FROM jobs ORDER BY created_at DESC LIMIT 10",
				).all();
				return text(results.length ? JSON.stringify(results, null, 2) : "No jobs yet.");
			},
		);

		server.registerTool(
			"list_episodes",
			{
				description: "Published episodes, newest first.",
				inputSchema: z.object({}),
				annotations: { readOnlyHint: true },
			},
			async () => {
				const obj = await env.BUCKET.get(`${env.FEED_PREFIX}/episodes.json`);
				const eps: any[] = obj ? await obj.json() : [];
				if (!eps.length) return text("No episodes yet.");
				return text(
					eps
						.reverse()
						.map((e) => `${e.pub_date.slice(0, 10)} | ${Math.round(e.duration / 60)} min | ${e.title}`)
						.join("\n"),
				);
			},
		);

		server.registerTool(
			"retry_job",
			{
				description:
					"Retry a failed job. The Mac resumes from the last stage that finished, so a script that " +
					"was already written is not rewritten.",
				inputSchema: z.object({ job_id: z.string() }),
			},
			async ({ job_id }) => {
				const { meta } = await env.DB.prepare(
					`UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, updated_at = datetime('now')
					 WHERE id = ? AND status IN ('failed', 'retrying')`,
				)
					.bind(job_id)
					.run();
				return text(
					meta.changes
						? `Requeued job ${job_id}. Devon's Mac picks it up within a minute when it is awake.`
						: `Job ${job_id} is not failed or retrying, so there is nothing to retry.`,
				);
			},
		);

		server.registerTool(
			"feed_url",
			{
				description: "The podcast feed URL to subscribe to.",
				inputSchema: z.object({}),
				annotations: { readOnlyHint: true },
			},
			async () => text(`${env.PUBLIC_BASE_URL}/${env.FEED_PREFIX}/feed.xml`),
		);

		return server;
	};
}

async function enqueue(env: Env, kind: string, payload: Record<string, unknown>): Promise<string> {
	const id = crypto.randomUUID().slice(0, 8);
	const title = typeof payload.title === "string" ? payload.title : (payload.url as string);
	await env.DB.prepare("INSERT INTO jobs (id, kind, payload, title) VALUES (?, ?, ?, ?)")
		.bind(id, kind, JSON.stringify(payload), title)
		.run();
	return id;
}

// --- Job API for the Mac poller -------------------------------------------------------

// The Mac confirms a claim within seconds by reporting the job's episode id ("accepted"). After
// that it owns the job durably and retries failures itself, so only unconfirmed claims, where the
// claim response was lost, are handed out again.
const STALE_CLAIM_MINUTES = 10;
const MAX_CLAIMS = 3;

async function claim(env: Env): Promise<Response> {
	const stale = `status = 'claimed' AND updated_at < datetime('now', '-${STALE_CLAIM_MINUTES} minutes')`;
	const [, claimed] = await env.DB.batch<{ id: string; kind: string; payload: string; episode_id: string | null }>([
		env.DB.prepare(
			`UPDATE jobs SET status = 'failed', updated_at = datetime('now'),
			 error = 'Claimed ${MAX_CLAIMS} times but the Mac never confirmed it'
			 WHERE ${stale} AND attempts >= ${MAX_CLAIMS}`,
		),
		env.DB.prepare(
			`UPDATE jobs SET status = 'claimed', attempts = attempts + 1, updated_at = datetime('now')
			 WHERE id = (
			   SELECT id FROM jobs
			   WHERE status = 'queued' OR (${stale} AND attempts < ${MAX_CLAIMS})
			   ORDER BY created_at LIMIT 1)
			 RETURNING id, kind, payload, episode_id`,
		),
	]);
	const job = claimed.results[0];
	if (!job) return new Response(null, { status: 204 });
	return Response.json({ ...job, payload: JSON.parse(job.payload) });
}

async function update(env: Env, id: string, request: Request): Promise<Response> {
	const body = (await request.json()) as Record<string, string | undefined>;
	const allowed = ["accepted", "rendering", "retrying", "published", "failed"];
	if (!body.status || !allowed.includes(body.status)) return new Response("bad status", { status: 400 });
	await env.DB.prepare(
		`UPDATE jobs SET status = ?, episode_id = COALESCE(?, episode_id), title = COALESCE(?, title),
		 error = ?, updated_at = datetime('now') WHERE id = ?`,
	)
		.bind(body.status, body.episode_id ?? null, body.title ?? null, body.error ?? null, id)
		.run();
	return Response.json({ ok: true });
}

async function authorized(request: Request, env: Env): Promise<boolean> {
	const got = new TextEncoder().encode(request.headers.get("Authorization") ?? "");
	const want = new TextEncoder().encode(`Bearer ${env.WORKER_TOKEN}`);
	return got.byteLength === want.byteLength && crypto.subtle.timingSafeEqual(got, want);
}

export default {
	async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
		const url = new URL(request.url);
		const mcpRoute = `/${env.MCP_PATH_SECRET}/mcp`;

		if (url.pathname === mcpRoute) {
			return createMcpHandler(createServer(env), {
				route: mcpRoute,
				allowedHostnames: [env.HOSTNAME],
			})(request, env, ctx);
		}

		if (url.pathname.startsWith("/api/jobs") && request.method === "POST") {
			if (!(await authorized(request, env))) return new Response("unauthorized", { status: 401 });
			if (url.pathname === "/api/jobs/claim") return claim(env);
			const m = url.pathname.match(/^\/api\/jobs\/([\w-]+)$/);
			if (m) return update(env, m[1], request);
		}

		return new Response("not found", { status: 404 });
	},
} satisfies ExportedHandler<Env>;
