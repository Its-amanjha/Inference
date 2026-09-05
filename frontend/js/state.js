export const state = {user:null, instanceId:'', instances:[], preferences:{theme:'light',page_size:25,error_notifications:true}};
export const pageSize = () => state.preferences.page_size;
export function applyPreferences(value = {}) {
  state.preferences = {theme:['light','dark','n8n'].includes(value.theme)?value.theme:'light',page_size:Number.isInteger(value.page_size)&&value.page_size>=10&&value.page_size<=100?value.page_size:25,error_notifications:value.error_notifications!==false};
}
export const can = role => ({viewer:0,operator:1,admin:2}[state.user?.role] ?? -1) >= ({viewer:0,operator:1,admin:2}[role] ?? 99);
export const instanceQuery = () => state.instanceId ? `?instance_id=${encodeURIComponent(state.instanceId)}` : '';
