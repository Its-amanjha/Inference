export const state = {user:null, instanceId:'', instances:[], preferences:{theme:'light',page_size:25,error_notifications:true}};
export const pageSize = () => state.preferences.page_size;
export function applyPreferences(value = {}) {
  state.preferences = {theme:['light','dark','n8n'].includes(value.theme)?value.theme:'light',page_size:Number.isInteger(value.page_size)&&value.page_size>=10&&value.page_size<=100?value.page_size:25,error_notifications:value.error_notifications!==false};
}
export const can = role => ({viewer:0,operator:1,admin:2}[state.user?.role] ?? -1) >= ({viewer:0,operator:1,admin:2}[role] ?? 99);
// Live events this tab caused itself, so they do not come back as notifications. Events carry no detail
// (the hub forwards invalidation metadata only), so each expected event is counted by name for 10 seconds.
const ownEvents = new Map();
export function expectOwnEvent(name, ms = 10000) {
  const entry = ownEvents.get(name), live = entry && entry.until > Date.now();
  ownEvents.set(name, {count: (live ? entry.count : 0) + 1, until: Date.now() + ms});
}
export function cancelOwnEvent(name) {
  const entry = ownEvents.get(name);
  if (entry && --entry.count <= 0) ownEvents.delete(name);
}
export function isOwnEvent(name) {
  const entry = ownEvents.get(name);
  if (!entry || entry.until <= Date.now()) { ownEvents.delete(name); return false; }
  if (--entry.count <= 0) ownEvents.delete(name);
  return true;
}
