'use strict';
window.teemerchant={
  async request(method,path,body,identity){
    const headers={'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf]').content};
    if(identity)headers['X-Merchant-ID']=identity;
    const response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
    if(response.status===401){location.replace('/login');throw Error('请重新登录');}
    const value=await response.json();return response.ok?{ok:true,value}:{ok:false,error:value.error||'操作失败'};
  },
  async oauth(action,body,identity){
    const result=await this.request('POST','/api/oauth/'+action,body,identity);
    if(result.ok&&action==='open')window.open(result.value.auth_url,'_blank','noopener,noreferrer');
    return result;
  },
  async copyText(text){
    if(navigator.clipboard)await navigator.clipboard.writeText(text);
    else{const input=document.createElement('textarea');input.value=text;document.body.append(input);input.select();try{if(!document.execCommand('copy'))throw Error('复制失败，请手动复制');}finally{input.remove();}}
    return {ok:true};
  }
};
