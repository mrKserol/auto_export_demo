(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  const form = document.getElementById("estimate-form");
  const formError = document.getElementById("form-error");
  const formSuccess = document.getElementById("form-success");
  const fallbackSubmit = document.getElementById("fallback-submit");
  let submitting = false;

  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.MainButton) {
      tg.MainButton.setText("Создать смету");
      tg.MainButton.show();
      tg.MainButton.onClick(() => form.requestSubmit());
    }
  }

  function clearErrors() {
    formError.hidden = true;
    formError.textContent = "";
    formSuccess.hidden = true;
    formSuccess.textContent = "";
    form.querySelectorAll(".field").forEach((f) => f.classList.remove("has-error"));
    form.querySelectorAll(".field-error").forEach((n) => (n.textContent = ""));
  }

  function showFieldError(name, msg) {
    const node = form.querySelector(`[data-error-for="${name}"]`);
    if (node) node.textContent = msg;
    const field = node && node.closest(".field");
    if (field) field.classList.add("has-error");
  }

  function haptic(type) {
    try { tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred(type); } catch (_) {}
  }

  function setLoading(isLoading) {
    submitting = isLoading;
    fallbackSubmit.disabled = isLoading;
    if (tg && tg.MainButton) {
      if (isLoading) { tg.MainButton.showProgress(); tg.MainButton.disable(); }
      else { tg.MainButton.hideProgress(); tg.MainButton.enable(); }
    }
  }

  function collectPayload() {
    const fd = new FormData(form);
    return {
      engine_power: Number(fd.get("engine_power")),
      exchange_rate: Number(fd.get("exchange_rate")),
      inspect_transport_price: fd.get("inspect_transport_price"),
      bank_commission: fd.get("bank_commission"),
      transit_declaration_price: fd.get("transit_declaration_price"),
      insurance_shipment: fd.get("insurance_shipment"),
      custom_clearing: fd.get("custom_clearing"),
      contractor_comission: fd.get("contractor_comission"),
      context_token: new URLSearchParams(window.location.search).get("token") || "",
      telegram_init_data: tg ? tg.initData || "" : "",
    };
  }

  function clientValidate(p) {
    const errs = {};
    if (!p.engine_power || !Number.isFinite(p.engine_power) || p.engine_power <= 0) errs.engine_power = "Укажите мощность > 0";
    if (!p.exchange_rate || !Number.isFinite(p.exchange_rate) || p.exchange_rate <= 0) errs.exchange_rate = "Укажите корректный курс > 0";
    const moneyFields = ["inspect_transport_price","bank_commission","transit_declaration_price","insurance_shipment","custom_clearing","contractor_comission"];
    moneyFields.forEach((f)=>{ const v=Number(p[f]); if (!Number.isFinite(v) || v<0) errs[f]="Недопустимое значение";});
    return errs;
  }

  async function submitForm() {
    if (submitting) return;
    clearErrors();
    const payload = collectPayload();
    if (!payload.telegram_init_data) { formError.hidden=false; formError.textContent="Откройте форму через кнопку в Telegram-боте."; haptic("error"); return; }
    if (!payload.context_token) { formError.hidden=false; formError.textContent="Ссылка недействительна."; haptic("error"); return; }
    const clientErrors = clientValidate(payload);
    if (Object.keys(clientErrors).length) { Object.entries(clientErrors).forEach(([k,m])=>showFieldError(k,m)); formError.hidden=false; formError.textContent="Проверьте заполнение формы"; haptic("error"); return; }
    setLoading(true);
    try {
      const res = await fetch("/api/estimates", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload) });
      const data = await res.json().catch(()=>({}));
      if (!res.ok || !data.ok) {
        const err = data.error || {};
        if (err.fields) Object.entries(err.fields).forEach(([k,m])=> showFieldError(k.split(".").pop(), m));
        formError.hidden=false; formError.textContent = err.message || "Не удалось сохранить смету";
        haptic("error");
        return;
      }
      formSuccess.hidden=false; formSuccess.textContent = data.message || "Смета сохранена";
      haptic("success");
      setTimeout(()=>{ try{ tg && tg.close && tg.close(); } catch(_){} }, 900);
    } catch(e) {
      formError.hidden=false; formError.textContent="Ошибка сети"; haptic("error");
    } finally { setLoading(false); }
  }

  form.addEventListener("submit", (e)=>{ e.preventDefault(); submitForm(); });
})();

