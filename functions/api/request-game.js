export async function onRequestPost(context) {
  let gameName;
  try {
    const body = await context.request.json();
    gameName = body.gameName;
  } catch {
    return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  if (!gameName || typeof gameName !== "string") {
    return new Response(JSON.stringify({ error: "Missing or invalid gameName" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  const response = await fetch(
    "https://api.github.com/repos/RasberryPhi-AT/gaming-website/dispatches",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${context.env.AUTOMATION_GITHUB_TOKEN}`,
        Accept: "application/vnd.github.v3+json",
        "Content-Type": "application/json",
        "User-Agent": "Cloudflare-Pages",
      },
      body: JSON.stringify({
        event_type: "add_game_request",
        client_payload: { gameName },
      }),
    }
  );

  if (response.ok) {
    return new Response(JSON.stringify({ success: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  const errorText = await response.text();
  return new Response(errorText, {
    status: response.status,
    headers: { "Content-Type": "application/json" },
  });
}
