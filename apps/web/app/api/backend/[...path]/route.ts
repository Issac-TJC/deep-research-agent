import { NextRequest } from "next/server";
export const dynamic = "force-dynamic";
const upstream = process.env.RESEARCH_API_INTERNAL || "http://127.0.0.1:18000";
async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const key = request.cookies.get("research_session")?.value;
  if (!key) return new Response("Authentication required", { status: 401 });
  const origin = request.headers.get("origin");
  if (
    request.method !== "GET" &&
    origin &&
    new URL(origin).host !== request.headers.get("host")
  )
    return new Response("Forbidden", { status: 403 });
  const { path } = await context.params;
  if (
    !path.length ||
    !["research-runs", "uploads", "sources", "evidence-spans"].includes(
      path[0],
    ) ||
    path.some((x) => !/^[-a-zA-Z0-9]+$/.test(x))
  )
    return new Response("Not found", { status: 404 });
  const headers: Record<string, string> = { Authorization: "Bearer " + key };
  for (const name of ["content-type", "idempotency-key", "last-event-id"]) {
    const value = request.headers.get(name);
    if (value) headers[name] = value;
  }
  let body: ArrayBuffer | undefined;
  if (request.method !== "GET") {
    if (Number(request.headers.get("content-length") || 0) > 21 * 1024 * 1024)
      return new Response("Upload too large", { status: 413 });
    const chunks: Uint8Array[] = [];
    let size = 0;
    if (request.body) {
      const reader = request.body.getReader();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > 21 * 1024 * 1024) {
          await reader.cancel();
          return new Response("Upload too large", { status: 413 });
        }
        chunks.push(value);
      }
    }
    const merged = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      merged.set(chunk, offset);
      offset += chunk.length;
    }
    body = merged.buffer;
  }
  try {
    const response = await fetch(
      upstream + "/" + path.join("/") + request.nextUrl.search,
      {
        method: request.method,
        headers,
        body,
        cache: "no-store",
        signal: request.signal,
      },
    );
    const resultHeaders = new Headers({ "Cache-Control": "no-store" });
    for (const name of ["content-type", "content-disposition"]) {
      const value = response.headers.get(name);
      if (value) resultHeaders.set(name, value);
    }
    return new Response(response.body, {
      status: response.status,
      headers: resultHeaders,
    });
  } catch {
    return new Response("Backend unavailable", { status: 503 });
  }
}
export { proxy as GET, proxy as POST };
