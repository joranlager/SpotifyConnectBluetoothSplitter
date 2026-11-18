// The one place that talks to the Python backend (same origin, JSON).
// State-changing requests must send Content-Type: application/json; the server
// refuses anything else, which is what keeps other websites from using it.

export async function api(method, path, body, signal) {
  const options = { method, headers: {}, signal };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    // empty or non-JSON body
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
