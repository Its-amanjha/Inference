const paths = {
  dashboard:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  workflows:'M5 4h5v5H5z M14 15h5v5h-5z M7.5 9v8.5H14 M10 6.5h6.5V15',
  errors:'M12 3 2 21h20L12 3z M12 9v5 M12 17v.1',
  observe:'M3 12h4l3-8 4 16 3-8h4',
  'fleet-health':'M3 6h18v5H3z M3 15h18v5H3z M6 8.5h.1 M6 17.5h.1',
  promote:'M12 20V4 M6 10l6-6 6 6 M4 20h16',
  codelab:'m8 6-6 6 6 6 M16 6l6 6-6 6 M14 3l-4 18',
  assistant:'M4 4h16v13H9l-5 4V4z M8 9h8 M8 13h4',
  containers:'m12 2 9 5v10l-9 5-9-5V7l9-5z M3 7l9 5 9-5 M12 12v10',
  notes:'M5 3h10l4 4v14H5V3z M15 3v5h4 M8 12h8 M8 16h6',
  backup:'M4 8V3 M4 8h5 M4 8a9 9 0 1 1-1 8 M12 7v6l4 2',
  secrets:'M7 10V7a5 5 0 0 1 10 0v3 M5 10h14v11H5z M12 14v3',
  instances:'M3 4h18v12H3z M8 21h8 M12 16v5',
  settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
  theme:'M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z',
  arrow:'M5 12h14 M13 6l6 6-6 6',
  refresh:'M20 8V3 M20 8h-5 M20 8A9 9 0 1 0 21 16',
  menu:'M4 6h16 M4 12h16 M4 18h16',
  add:'M12 5v14 M5 12h14',
  save:'M4 3h13l4 4v14H3V3h1 M7 3v6h9V3 M7 21v-8h10v8',
  edit:'m15 4 5 5 M4 15 16 3l5 5-12 12-6 1 1-6z',
  delete:'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7',
  play:'m8 4 12 8-12 8V4z',
  stop:'M5 5h14v14H5z',
  check:'m4 12 5 5L20 6',
  info:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 11v5 M12 8v.1',
  close:'m6 6 12 12 M6 18 18 6',
  download:'M12 3v12 M7 10l5 5 5-5 M4 17v4h16v-4',
  search:'M16 10a6 6 0 1 1-12 0 6 6 0 0 1 12 0z M15 15l6 6',
  logout:'M9 3H3v18h6 M9 12h12 M16 7l5 5-5 5',
};
// Views may add their own glyphs without editing the shared map.
export function registerIcons(map) {
  for(const [name,value] of Object.entries(map))if(!(name in paths))paths[name]=value;
}
export function actionIcon(label) {
  if(typeof label!=='string')return null;
  const match=[[/^(Refresh|Test connection)/i,'refresh'],[/^(Save|Create admin)/i,'save'],[/^(Add|New|Create)/i,'add'],[/^Edit/i,'edit'],[/^(Delete|Remove|Purge)/i,'delete'],[/^(Activate|Start|Run now)/i,'play'],[/^(Deactivate|Stop|Disable)/i,'stop'],[/^(Confirm|Mark resolved|Enable)/i,'check'],[/^Cancel/i,'close'],[/^(Download|Export)/i,'download'],[/^(Inspect|Search|Discover)/i,'search'],[/^Sign out/i,'logout'],[/^(Deploy|Promote)/i,'promote']].find(([pattern])=>pattern.test(label));
  return match?icon(match[1]):null;
}
export function icon(name) {
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [key,value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.6','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',class:'icon'}))svg.setAttribute(key,value);
  const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',paths[name]||paths.dashboard);svg.append(path);return svg;
}
