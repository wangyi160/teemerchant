"use strict";
const register=document.body.dataset.page==='register';
const form=$('#auth-form'), submit=form.querySelector('[type=submit]');
const authRole=document.body.dataset.authRole;
let policyReady=!register;
document.querySelectorAll('[data-auth-role-link]').forEach(link=>{if(link.dataset.authRoleLink===authRole)link.setAttribute('aria-current','page');});
function updateApproval(policy){
  $('#approval-hint').textContent=authRole==='merchant'
    ?'提交后由管理员审核。通过后可领取 Merchant ID 和 Merchant Secret，用于连接 teemerchant。'
    :policy.user_approval==='manual'?'提交后由管理员审核，可在用户中心查看进度。':'注册后自动开通，可在用户中心创建 API key。';
}
async function loadPolicy(){
  submit.disabled=true;
  try{
    const {policy}=await api('info');
    policyReady=policy.registration_enabled;
    $('#registration-notice').hidden=policyReady;
    updateApproval(policy);
  }catch(error){form.querySelector('.error').textContent=error.message;}
  finally{submit.disabled=!policyReady;}
}
form.onsubmit=async event=>{
  event.preventDefault();if(!policyReady)return;
  submit.disabled=true;form.querySelector('.error').textContent='';
  try{
    const data=Object.fromEntries(new FormData(form));
    if(register)data.role=authRole;
    const result=await api(register?'register':'login','POST',data);
    form.reset();location.replace(workspacePath(result.account.role));
  }catch(error){form.querySelector('.error').textContent=error.message;submit.disabled=!policyReady;}
};
if(register)loadPolicy();
window.addEventListener('pageshow',event=>{
  if(event.persisted){
    form.reset();api('me').then(result=>{if(!register||result.account.role===authRole)location.replace(workspacePath(result.account.role));}).catch(()=>{});
  }
});
