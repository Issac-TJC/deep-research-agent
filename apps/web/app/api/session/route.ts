import { NextRequest, NextResponse } from "next/server";
const upstream = process.env.RESEARCH_API_INTERNAL || "http://127.0.0.1:18000";
export async function POST(request: NextRequest) {
  const origin = request.headers.get("origin");
  if (origin && new URL(origin).host !== request.headers.get("host"))
    return new Response("Forbidden", { status: 403 });
  const { key } = await request.json();
  if (typeof key !== "string" || key.length > 200)
    return new Response("Invalid key", { status: 400 });
  try {
    const response = await fetch(upstream + "/research-runs", {
      headers: { Authorization: "Bearer " + key },
      cache: "no-store",
    });
    if (!response.ok)
      return new Response("API key validation failed", {
        status: response.status,
      });
    const result = NextResponse.json({ ok: true });
    result.cookies.set("research_session", key, {
      httpOnly: true,
      sameSite: "strict",
      secure: request.nextUrl.protocol === "https:",
      path: "/",
      maxAge: 28800,
    });
    return result;
  } catch {
    return new Response("Backend unavailable", { status: 503 });
  }
}
export async function DELETE() {
  const result = NextResponse.json({ ok: true });
  result.cookies.delete("research_session");
  return result;
}
