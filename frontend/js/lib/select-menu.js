// A single-choice dropdown: a button showing the current choice, and a list of options with icons,
// state dots and secondary text. Used where a plain <select> cannot show what the choice is, such as
// the instance switcher and CodeLab's node chooser. The list is a popover in the top layer (no card
// or table clips it), it animates open, and it follows the listbox keyboard pattern: Enter, Space or
// the arrow keys open it; arrows, Home, End and first letters move; Enter or Space chooses; Escape
// or Tab closes; focus returns to the button.
import {el} from './html.js';
import {icon} from './icons.js';

let sequence=0;

// options: {value, label, detail?, glyph?, dot?: 'up'|'down'|'unknown'}
export function selectMenu({label,options=[],value='',onChange,className=''}={}){
  const id=`select-menu-${++sequence}`;
  let items=[],current=value;
  const shown=el('span',{class:'select-menu-value'});
  const lead=el('span',{class:'select-menu-lead','aria-hidden':'true'});
  const button=el('button',{type:'button',class:`select-menu-button ${className}`.trim(),'aria-haspopup':'listbox','aria-expanded':'false','aria-controls':id,popovertarget:id},
    lead,shown,el('span',{class:'select-menu-chevron','aria-hidden':'true'},icon('chevron')));
  const list=el('ul',{id,class:'select-menu-list',role:'listbox','aria-label':label,popover:'auto'});
  const marker=option=>option?.dot?el('span',{class:`select-menu-dot is-${option.dot}`}):option?.glyph?icon(option.glyph):null;

  function paintButton(){
    const option=items.find(item=>item.value===current)||items[0];
    shown.textContent=option?.label||'';
    lead.replaceChildren(...[marker(option)].filter(Boolean));
    button.setAttribute('aria-label',`${label}: ${option?.label||'none'}`);
    button.title=option?.detail?`${option.label} · ${option.detail}`:option?.label||'';
  }
  function paintList(){
    list.replaceChildren(...items.map((option,index)=>{
      const selected=option.value===current;
      const node=el('li',{role:'option',id:`${id}-${index}`,tabindex:'-1',class:'select-menu-option','aria-selected':String(selected),'data-value':option.value},
        el('span',{class:'select-menu-lead','aria-hidden':'true'},marker(option)),
        el('span',{class:'select-menu-text'},el('span',{class:'select-menu-label'},option.label),option.detail?el('span',{class:'select-menu-detail'},option.detail):null),
        selected?el('span',{class:'select-menu-check','aria-hidden':'true'},icon('check')):null);
      node.addEventListener('click',()=>choose(option.value));
      return node;
    }));
  }
  function choose(next){
    list.hidePopover?.();
    button.focus();
    if(next===current)return;
    current=next;paintButton();paintList();
    onChange?.(current);
  }
  const place=()=>{
    const box=button.getBoundingClientRect(),width=Math.max(list.offsetWidth,box.width),height=list.offsetHeight;
    list.style.minWidth=`${Math.round(box.width)}px`;
    const left=Math.max(8,Math.min(box.left,innerWidth-width-8));
    const below=box.bottom+4+height<=innerHeight-8;
    list.style.left=`${left}px`;list.style.top=`${below?box.bottom+4:Math.max(8,box.top-4-height)}px`;
  };
  const close=()=>list.hidePopover?.();
  const options_=()=>[...list.querySelectorAll('[role=option]')];
  list.addEventListener('toggle',event=>{
    const open=event.newState==='open';
    button.setAttribute('aria-expanded',String(open));
    if(open){
      place();
      (options_().find(node=>node.getAttribute('aria-selected')==='true')||options_()[0])?.focus();
      addEventListener('scroll',close,{capture:true,passive:true});addEventListener('resize',close);
    }else{
      removeEventListener('scroll',close,{capture:true});removeEventListener('resize',close);
      if(list.contains(document.activeElement)||document.activeElement===document.body)button.focus();
    }
  });
  button.addEventListener('keydown',event=>{
    if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();list.showPopover?.();}
  });
  list.addEventListener('keydown',event=>{
    const nodes=options_(),index=nodes.indexOf(document.activeElement);
    const move=to=>{event.preventDefault();event.stopPropagation();nodes[Math.max(0,Math.min(nodes.length-1,to))]?.focus();};
    if(event.key==='ArrowDown')move(index+1);
    else if(event.key==='ArrowUp')move(index-1);
    else if(event.key==='Home')move(0);
    else if(event.key==='End')move(nodes.length-1);
    else if(event.key==='Enter'||event.key===' '){event.preventDefault();event.stopPropagation();if(index>=0)choose(nodes[index].dataset.value);}
    else if(event.key==='Tab'){event.preventDefault();close();}
    else if(event.key==='Escape')event.stopPropagation();
    else if(event.key.length===1&&/\S/.test(event.key)){
      // Typeahead: the next option whose label starts with the typed letter.
      const letter=event.key.toLowerCase(),order=[...nodes.slice(index+1),...nodes.slice(0,index+1)];
      const hit=order.find(node=>node.textContent.trim().toLowerCase().startsWith(letter));
      if(hit){event.preventDefault();event.stopPropagation();hit.focus();}
    }
  });

  function setOptions(next,value=current){
    items=next.map(option=>({...option,value:String(option.value)}));
    current=items.some(item=>item.value===String(value))?String(value):(items[0]?.value??'');
    paintButton();paintList();
  }
  setOptions(options,value);
  return {node:el('span',{class:'select-menu'},button,list),button,setOptions,
    get value(){return current;},
    set value(next){current=String(next);paintButton();paintList();},
    set disabled(flag){button.disabled=Boolean(flag);}};
}
