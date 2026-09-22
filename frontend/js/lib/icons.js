import {lucideIcons} from './lucide-icons.js';
const paths = {...lucideIcons};
// Views may add their own glyphs without editing the shared map.
export function registerIcons(map) {
  for(const [name,value] of Object.entries(map))if(!(name in paths))paths[name]=value;
}
export function actionIcon(label) {
  if(typeof label!=='string')return null;
  const match=[[/^(Connect model|Connect provider)/i,'test'],[/^(Providers and remote tools)/i,'settings'],[/^(Context and privacy|What the AI sees)/i,'shield'],[/^(Assistant settings|Configure|Settings)/i,'settings'],[/^(AI workspace|Explain|Repair|Continue)/i,'assistant'],[/^(Code editor)/i,'codelab'],[/^(Check|Re-check|Test)/i,'test'],[/^(Provision)/i,'containers'],[/^(Preview|View|Inspect)/i,'eye'],[/^(Open|Load|Resume)/i,'arrow'],[/^(Retry|Reconcile|Restore)/i,'refresh'],[/^(Send)/i,'send'],[/^(Copy)/i,'copy'],[/^(Clear|Reset)/i,'close'],[/^(Filter)/i,'filter'],[/^(Refresh|Test connection)/i,'refresh'],[/^(Save|Create admin)/i,'save'],[/^(Add|New|Create)/i,'add'],[/^Edit/i,'edit'],[/^(Delete|Remove|Purge)/i,'delete'],[/^(Activate|Start|Run now)/i,'play'],[/^(Deactivate|Stop|Disable)/i,'stop'],[/^(Confirm|Mark resolved|Enable)/i,'check'],[/^Cancel/i,'close'],[/^(Download|Export)/i,'download'],[/^(Inspect|Search|Discover)/i,'search'],[/^Sign out/i,'logout'],[/^(Deploy|Promote)/i,'promote']].find(([pattern])=>pattern.test(label));
  return match?icon(match[1]):null;
}
export function icon(name) {
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',class:'icon'}))svg.setAttribute(key,value);
  const shape=paths[name]||paths.dashboard;
  const nodes=typeof shape==='string'?[['path',{d:shape}]]:shape;
  for(const [tag,attrs] of nodes){const node=document.createElementNS(svg.namespaceURI,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);svg.append(node);}
  return svg;
}
