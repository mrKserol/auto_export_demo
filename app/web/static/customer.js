(function(){
  const tg = window.Telegram && window.Telegram.WebApp;
  const form = document.getElementById("customer-form");
  const fallback = document.getElementById("fallback-submit");
  const formError = document.getElementById("form-error");
  const formSuccess = document.getElementById("form-success");
  const formWarnings = document.getElementById("form-warnings");
  let submitting = false;

  function getContextToken(){ const params=new URLSearchParams(window.location.search); return (params.get("token")||"").trim(); }
  function clearMessages(){ if(formError) formError.hidden=true; if(formSuccess) formSuccess.hidden=true; if(formWarnings) formWarnings.hidden=true; }
  function showError(msg){ if(formError){ formError.hidden=false; formError.textContent=msg; } }
  function showSuccess(msg){ if(formSuccess){ formSuccess.hidden=false; formSuccess.textContent=msg; } }
  function showWarnings(items){
    if(!formWarnings) return;
    if(!items || !items.length){ formWarnings.hidden=true; formWarnings.textContent=""; return; }
    formWarnings.hidden=false;
    formWarnings.textContent = "Предупреждения:\n" + items.map((item)=>"• "+item).join("\n");
  }

  const isTelegram = Boolean(tg && tg.initData);
  if (isTelegram) {
    tg.ready();
    tg.expand();
    if (tg.MainButton) {
      tg.MainButton.setText("Сохранить данные");
      tg.MainButton.show();
    }
    if (fallback) fallback.hidden = true;
  } else {
    // show fallback button in non-Telegram
    if (fallback) fallback.hidden = false;
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
      showWarnings(data.warnings || []);
      if (data.mode === "create_from_batch" && tg && tg.MainButton) {
        tg.MainButton.setText("Сохранить клиента");
      }
    }catch(e){ showError("Ошибка сети при загрузке"); }
    finally{ setLoading(false); }
  }

  function setLoading(v) {
    submitting = v;
    if (fallback) fallback.disabled = v;
    if (isTelegram && tg && tg.MainButton) {
      if (v) {
        tg.MainButton.showProgress();
        tg.MainButton.disable();
      } else {
        tg.MainButton.hideProgress();
        tg.MainButton.enable();
      }
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

  let isSubmitting = false;

  async function submitCustomer() {
    if (isSubmitting) return;
    isSubmitting = true;
    clearMessages();
    const token = getContextToken();
    if (!token) {
      showError("Откройте форму через кнопку в боте.");
      isSubmitting = false;
      return;
    }
    if (!isTelegram) {
      // allow browser submit but require no initData
    } else if (!tg || !tg.initData) {
      showError("Откройте форму из Telegram.");
      isSubmitting = false;
      return;
    }
    const payload = collect();
    setLoading(true);
    try {
      const res = await fetch("/api/customers", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        const err = data.error || {};
        if (err.fields) {
          const first = Object.values(err.fields)[0];
          showError(first || err.message || "Ошибка сохранения");
        } else if (err.code === "PASSPORT_ALREADY_EXISTS") {
          showError(err.message || "Паспорт уже существует");
        } else showError(err.message || "Ошибка сохранения");
        return;
      }
      showSuccess(data.message || "Данные клиента сохранены");
      try {
        if (isTelegram && tg && typeof tg.HapticFeedback !== "undefined") tg.HapticFeedback.notificationOccurred("success");
      } catch (_e) {}
      setTimeout(() => {
        try { if (isTelegram && tg && typeof tg.close === "function") tg.close(); } catch (_){}
      }, 1200);
    } catch (e) {
      showError("Ошибка сети");
    } finally {
      setLoading(false);
      isSubmitting = false;
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitCustomer();
  });
  if (isTelegram && tg && tg.MainButton) {
    // bind MainButton to submitCustomer
    try {
      tg.MainButton.onClick(submitCustomer);
    } catch (_e) {
      // ignore
    }
  }
  // fallback button is type=submit so no click listener needed
  // init
  loadContext();
})();

