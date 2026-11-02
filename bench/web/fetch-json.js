// A JSON answer that is read completely or not at all. An intermediary (relay, proxy) can cut a long answer
// at a buffer size and end the body cleanly: the server's figures are then the only witness.
// `init.peek`: add the first and last characters to the diagnostic (only for answers known to hold nothing secret).
export async function fetchJson(url, init = {}, doFetch = fetch) {
  const { peek = false, ...rest } = init;
  const r = await doFetch(url, rest);
  const bytes = new Uint8Array(await r.arrayBuffer());
  const announced = r.headers.get("content-length");
  const where = String(url).split("?")[0];
  const facts = `${where} HTTP ${r.status} content-type=${r.headers.get("content-type") ?? "absent"} content-length=${announced ?? "absent"} reçu=${bytes.length} octets`;
  const text = new TextDecoder().decode(bytes);
  const ends = peek ? ` ; début "${text.slice(0, 24)}" fin "${text.slice(-24)}"` : "";
  if (!r.ok) throw new Error(`réponse refusée : ${facts}${ends}`);
  if (announced !== null && Number(announced) !== bytes.length) throw new Error(`réponse incomplète : ${facts}${ends}`);
  try { return JSON.parse(text); }
  catch (e) { throw new Error(`réponse illisible (tronquée ?) : ${facts}${ends}`); }
}
