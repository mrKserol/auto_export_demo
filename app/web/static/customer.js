(function(){
  const tg = window.Telegram && window.Telegram.WebApp;
  const form = document.getElementById("customer-form");
  const fallback = document.getElementById("fallback-submit");
  const formError = document.getElementById("form-error");
  const formSuccess = document.getElementById("form-success");
  let submitting = false;

  function getContextToken(){ const params=new URLSearchParams(window.location.search); return (params.get("token")||"").trim(); }
  function clearMessages(){ if(formError) formError.hidden=true; if(formSuccess) formSuccess.hidden=true; }
  function showError(msg){ if(formError){ formError.hidden=false; formError.textContent=msg; } }
  function showSuccess(msg){ if(formSuccess){ formSuccess.hidden=false; formSuccess.textContent=msg; } }

  if(tg){
    tg.ready(); tg.expand();
    if(tg.MainButton){ tg.MainButton.setText("Сохранить данные"); tg.MainButton.show(); tg.MainButton.onClick(()=>form.requestSubmit()); }
  } else {
    // show fallback button in non-Telegram
    if(fallback) fallback.classList.remove("hidden");
  }

  async function loadContext(){
    clearMessages();
    const token = getContextToken();
    if(!token){
      showError("Откройте форму через кнопку в боте.");
      return;
    }
    if(!tg || !tg.initData){
      showError("Откройте форму из Telegram.");
      return;
    }
    setLoading(true);
    try{
      const res = await fetch("/api/customers/edit-context", { method:"POST", headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ context_token: token, telegram_init_data: tg.initData || "" }) });
      const data = await res.json().catch(()=>({}));
      if(!res.ok || !data.ok){ showError(data.error?.message || "Не удалось загрузить данные клиента"); return; }
      const vals = data.values || {};
      Object.entries(vals).forEach(([k,v])=>{
        const el = form.querySelector(`[name="${k}"]`);
        if(el && v !== null && v !== undefined) el.value = v;
      });
    }catch(e){ showError("Ошибка сети при загрузке"); }
    finally{ setLoading(false); }
  }

  function setLoading(v){
    submitting = v;
    if(fallback) fallback.disabled=v;
    if(tg && tg.MainButton){
      if(v){ tg.MainButton.showProgress(); tg.MainButton.disable(); } else { tg.MainButton.hideProgress(); tg.MainButton.enable(); }
    }
  }

  function collect(){
    const fd = new FormData(form);
    const obj = {};
    for(const [k,v] of fd.entries()) obj[k]=v;
    obj.context_token = getContextToken();
    obj.telegram_init_data = tg?tg.initData||"": "";
    return obj;
  }

  async function submit(){
    if(submitting) return;
    clearMessages();
    const token = getContextToken();
    if(!token){ showError("Откройте форму через кнопку в боте."); return; }
    if(!tg || !tg.initData){ showError("Откройте форму из Telegram."); return; }
    const payload = collect();
    setLoading(true);
    try{
      const res = await fetch("/api/customers", { method:"PUT", headers:{"Content-Type":"application/json"}, body: JSON.stringify(payload) });
      const data = await res.json().catch(()=>({}));
      if(!res.ok || !data.ok){
        const err = data.error || {};
        if(err.fields){
          // show first field error
          const first = Object.values(err.fields)[0];
          showError(first || err.message || "Ошибка сохранения");
        } else showError(err.message || "Ошибка сохранения");
        return;
      }
      showSuccess(data.message || "Данные клиента сохранены");
      try{ if(tg && typeof tg.HapticFeedback !== "undefined") tg.HapticFeedback.notificationOccurred("success"); }catch(_){}
      setTimeout(()=>{ try{ if(tg && typeof tg.close==="function") tg.close(); }catch(_){ } }, 1200);
    }catch(e){
      showError("Ошибка сети");
    }finally{ setLoading(false); }
  }

  form.addEventListener("submit", e=>{ e.preventDefault(); submit(); });
  if(fallback) fallback.addEventListener("click", ()=> submit());
  // init
  loadContext();
})();

