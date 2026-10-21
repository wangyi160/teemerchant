'use strict';
document.querySelector('#login').onsubmit=async e=>{
  e.preventDefault();const form=e.target,button=form.querySelector('button');button.disabled=true;
  try{
    const response=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name=csrf]').content},body:JSON.stringify(Object.fromEntries(new FormData(form)))});
    const result=await response.json();if(!response.ok)throw Error(result.error||'登录失败');location.replace('/');
  }catch(error){document.querySelector('#error').textContent=error.message;}
  finally{button.disabled=false;}
};
