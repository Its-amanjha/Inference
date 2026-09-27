import {lucideIcons} from './lucide-icons.js';
const paths = {...lucideIcons};
// Views may add their own glyphs without editing the shared map.
export function registerIcons(map) {
  for(const [name,value] of Object.entries(map))if(!(name in paths))paths[name]=value;
}
// Shared by row menus and every "Open in n8n" link.
registerIcons({
  wrench:'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z',
  more:[['circle',{cx:'5',cy:'12',r:'1.2'}],['circle',{cx:'12',cy:'12',r:'1.2'}],['circle',{cx:'19',cy:'12',r:'1.2'}]],
  external:'M15 3h6v6 M10 14 21 3 M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
});
export function actionIcon(label) {
  if(typeof label!=='string')return null;
  const match=[[/^(Connect model|Connect provider)/i,'test'],[/^(Providers and remote tools)/i,'settings'],[/^(Context and privacy|What the AI sees)/i,'shield'],[/^(Assistant settings|Configure|Settings)/i,'settings'],
    // Each verb gets its own glyph: previewing changes, checking again and testing a connection are different acts.
    [/^Repair/i,'wrench'],[/^(AI workspace|Conversation|Explain|Continue)/i,'assistant'],[/^(Code editor|CodeLab)/i,'codelab'],[/^Check changes/i,'eye'],[/^(Check again|Check fleet|Check target state|Check for new|Reload|Refresh|Retry|Reconcile|Restore)/i,'refresh'],
    [/^(Check tool calling|Re-check|Test)/i,'test'],[/^(Provision)/i,'containers'],[/^(Preview|View|Inspect|Details)/i,'eye'],[/^Shortcuts/i,'help'],[/^Replace/i,'edit'],[/^Undo/i,'history'],[/^(Open|Load|Resume)/i,'arrow'],
    [/^(Send)/i,'send'],[/^(Copy)/i,'copy'],[/^(Clear|Reset)/i,'close'],[/^(Filter)/i,'filter'],[/^(Save|Create admin)/i,'save'],[/^(Add|New|Create)/i,'add'],[/^Edit/i,'edit'],[/^(Delete|Remove|Purge)/i,'delete'],
    [/^(Activate|Start|Run now)/i,'play'],[/^(Deactivate|Stop|Disable)/i,'stop'],[/^(Confirm|Mark resolved|Enable|Done)/i,'check'],[/^Cancel/i,'close'],[/^(Download|Export)/i,'download'],[/^(Search|Discover)/i,'search'],[/^Sign out/i,'logout'],[/^(Deploy|Promote)/i,'promote']].find(([pattern])=>pattern.test(label));
  return match?icon(match[1]):null;
}
export function icon(name) {
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.8','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',class:'icon'}))svg.setAttribute(key,value);
  // An unknown name draws nothing rather than a misleading dashboard glyph.
  const shape=paths[name]||[];
  const nodes=typeof shape==='string'?[['path',{d:shape}]]:shape;
  for(const [tag,attrs] of nodes){const node=document.createElementNS(svg.namespaceURI,tag);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,value);svg.append(node);}
  return svg;
}
