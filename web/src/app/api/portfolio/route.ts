import { userSend } from "@/lib/api/user";

/** Delete the signed-in visitor's whole portfolio. */
export async function DELETE(request: Request): Promise<Response> {
  if (request.headers.get("origin") !== new URL(request.url).origin) {
    return Response.json({ detail: "Not allowed." }, { status: 403 });
  }
  return userSend("/me/portfolio", { method: "DELETE" }, request.headers);
}
