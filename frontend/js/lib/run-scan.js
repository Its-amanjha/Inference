// Reading new finished runs from n8n records their AI cost and their failures. The views that show either
// (Overview, Errors, Observability) ask for it as they open; a check made in the last minute is not repeated
// unless someone asks for it. One instance at a time: n8n answers one request at a time per instance anyway.
const RECENT_MS = 60000;
const checked = new Map();

// Each result is {instance, result} (the server's answer), {instance, error} or {instance, skipped: true}.
// With `cursors`, only instances that have one are checked, for their older runs.
export async function checkRuns(api, instanceIds, {signal, force = false, cursors = null} = {}) {
  const results = [];
  for (const instance of instanceIds) {
    const cursor = cursors ? cursors.get(instance) : null;
    if (cursors && !cursor) continue;
    if (!cursors && !force && Date.now() - (checked.get(instance) || 0) < RECENT_MS) {
      results.push({instance, skipped: true});
      continue;
    }
    try {
      const result = await api('/api/otel/runs/scan', {method: 'POST', signal, json: {instance_id: instance, ...(cursor ? {cursor} : {})}});
      if (!cursor) checked.set(instance, Date.now());
      results.push({instance, result});
    } catch (error) {
      if (signal?.aborted || error.name === 'AbortError') throw error;
      results.push({instance, error});
    }
  }
  return results;
}
