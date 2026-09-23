// A row's secondary actions behind one "More actions" button. The list is a popover in the top
// layer, so a scrolling table cannot clip it; it opens under the button and flips above near the
// bottom of the window. Arrow keys move between items, Escape or a click outside closes it, and
// focus returns to the button.
import {el} from './html.js';
import {icon,registerIcons} from './icons.js';

registerIcons({
  more:[['circle',{cx:'5',cy:'12',r:'1.2'}],['circle',{cx:'12',cy:'12',r:'1.2'}],['circle',{cx:'19',cy:'12',r:'1.2'}]],
  external:'M15 3h6v6 M10 14 21 3 M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6',
});

let sequence=0;

// items: {label, onSelect, href, external, danger, disabled}; falsy items are skipped.
export function rowMenu(items,label='More actions'){
  const id=`row-menu-${++sequence}`;
  const trigger=el('button',{type:'button',class:'row-menu-trigger','aria-label':label,title:label,'aria-haspopup':'menu','aria-expanded':'false','aria-controls':id,popovertarget:id},icon('more'));
  const list=el('div',{id,class:'row-menu',role:'menu','aria-label':label,popover:'auto'});
  const entries=items.filter(Boolean).map(item=>{
    const attrs={role:'menuitem',tabindex:'-1',class:`row-menu-item${item.danger?' is-danger':''}`};
    const node=item.href
      ?el('a',{...attrs,href:item.href,...(item.external?{target:'_blank',rel:'noopener noreferrer'}:{})},item.label,item.external?icon('external'):null)
      :el('button',{...attrs,type:'button',disabled:item.disabled},item.label);
    // el() gives buttons an action glyph; menu items stay text-only so buttons and links line up.
    if(!item.href)node.querySelector(':scope > svg')?.remove();
    node.addEventListener('click',()=>{list.hidePopover?.();if(!item.href)item.onSelect?.();});
    return node;
  });
  list.append(...entries);
  const enabled=()=>entries.filter(node=>!node.disabled);
  const place=()=>{
    const box=trigger.getBoundingClientRect(),width=list.offsetWidth,height=list.offsetHeight;
    const left=Math.max(8,Math.min(box.right-width,innerWidth-width-8));
    const below=box.bottom+4+height<=innerHeight-8;
    list.style.left=`${left}px`;list.style.top=`${below?box.bottom+4:Math.max(8,box.top-4-height)}px`;
  };
  // popovertarget makes the button the list's invoker, so clicking it again closes the list rather than
  // reopening it. The list is placed once when it opens, so scrolling the page closes it instead of leaving it behind.
  const close=()=>list.hidePopover?.();
  list.addEventListener('toggle',event=>{
    const open=event.newState==='open';
    trigger.setAttribute('aria-expanded',String(open));
    if(open){place();enabled()[0]?.focus();addEventListener('scroll',close,{capture:true,passive:true});addEventListener('resize',close);}
    else{removeEventListener('scroll',close,{capture:true});removeEventListener('resize',close);}
    if(!open&&(list.contains(document.activeElement)||document.activeElement===document.body))trigger.focus();
  });
  list.addEventListener('keydown',event=>{
    const nodes=enabled(),index=nodes.indexOf(document.activeElement);
    const move=to=>{event.preventDefault();event.stopPropagation();nodes[(to+nodes.length)%nodes.length]?.focus();};
    if(event.key==='ArrowDown')move(index+1);
    else if(event.key==='ArrowUp')move(index-1);
    else if(event.key==='Home')move(0);
    else if(event.key==='End')move(nodes.length-1);
    else if(event.key==='Tab')list.hidePopover();
    else if(event.key==='Escape'){event.stopPropagation();}
  });
  // The popover lives next to its trigger so the pair leaves the page together.
  return el('span',{class:'row-menu-wrap'},trigger,list);
}
