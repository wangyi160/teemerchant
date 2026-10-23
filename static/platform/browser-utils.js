"use strict";
function newOperationId(){
  if(typeof crypto.randomUUID==='function')return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join('');
}
async function copyToClipboard(value){
  if(navigator.clipboard?.writeText){
    try{await navigator.clipboard.writeText(String(value));return;}catch{}
  }
  const active=document.activeElement,buffer=document.createElement('textarea');
  buffer.value=String(value);buffer.readOnly=true;buffer.className='clipboard-buffer';
  (document.querySelector('dialog[open]')||document.body).appendChild(buffer);
  try{
    buffer.focus({preventScroll:true});buffer.select();
    if(!document.execCommand('copy'))throw Error('复制失败，请手动复制');
  }finally{buffer.remove();active?.focus({preventScroll:true});}
}
