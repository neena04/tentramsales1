// Triggers the "Refresh sales tables" workflow.
//
// The dashboard is a public page, so it must never carry a GitHub token. The token
// lives here as a Vercel environment variable and never reaches the browser; the page
// only sends a shared password, which is checked in constant time.
//
// Required Vercel environment variables:
//   GH_TOKEN          fine-grained PAT for neena04/tentramsales1 with Actions: read+write
//   REFRESH_PASSWORD  shared password the team types into the dashboard
//
// Run status is polled client-side straight from GitHub's public API, so no secret is
// needed for that half.

const OWNER = "neena04";
const REPO = "tentramsales1";
const WORKFLOW = "refresh.yml";

function safeEqual(a, b) {
  const A = Buffer.from(String(a));
  const B = Buffer.from(String(b));
  if (A.length !== B.length) return false;
  let diff = 0;
  for (let i = 0; i < A.length; i++) diff |= A[i] ^ B[i];
  return diff === 0;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Gunakan POST" });
  }

  const token = process.env.GH_TOKEN;
  const expected = process.env.REFRESH_PASSWORD;
  if (!token || !expected) {
    return res.status(500).json({
      error: "Server belum dikonfigurasi — GH_TOKEN / REFRESH_PASSWORD belum diset di Vercel",
    });
  }

  let body = req.body;
  if (typeof body === "string") {
    try { body = JSON.parse(body); } catch { body = {}; }
  }
  if (!safeEqual((body && body.password) || "", expected)) {
    // brief delay so the endpoint is not a fast password oracle
    await new Promise((r) => setTimeout(r, 700));
    return res.status(401).json({ error: "Password salah" });
  }

  // Refuse to queue a second run while one is already going.
  try {
    const runs = await fetch(
      `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/runs?per_page=1`,
      { headers: { Accept: "application/vnd.github+json", Authorization: `Bearer ${token}` } }
    );
    if (runs.ok) {
      const d = await runs.json();
      const latest = d.workflow_runs && d.workflow_runs[0];
      if (latest && latest.status !== "completed") {
        return res.status(409).json({
          error: "Refresh sedang berjalan, tunggu sebentar",
          runUrl: latest.html_url,
        });
      }
    }
  } catch {
    // if the check fails, fall through and try to dispatch anyway
  }

  const gh = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    }
  );

  if (gh.status !== 204) {
    const text = await gh.text();
    return res.status(502).json({
      error: `GitHub menolak permintaan (HTTP ${gh.status})`,
      detail: text.slice(0, 300),
    });
  }

  return res.status(202).json({ ok: true });
}
