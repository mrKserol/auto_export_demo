(function(){
  const tg = window.Telegram && window.Telegram.WebApp;
  const form = document.getElementById("customer-form");
  const fallback = document.getElementById("fallback-submit");
  const formError = document.getElementById("form-error");
  const formSuccess = document.getElementById("form-success");
  const formWarnings = document.getElementById("form-warnings");
  const documentsSection = document.getElementById("documents-section");
  const documentsList = document.getElementById("documents-list");
  const kitSummary = document.getElementById("kit-summary");
  let submitting = false;
  let formMode = "edit";
  let batchId = null;
  let alreadySaved = false;

  const DOCUMENT_TYPE_OPTIONS = [
    { value: "passport_main", label: "Паспорт" },
    { value: "passport_registration", label: "Прописка" },
    { value: "snils", label: "СНИЛС" },
    { value: "tin", label: "ИНН" },
    { value: "unknown", label: "Не определён" },
    { value: "mixed", label: "Смешанный документ" },
  ];

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
    if (fallback) fallback.hidden = false;
  }

  function fillValues(vals){
    Object.entries(vals || {}).forEach(([k,v])=>{
      const el = form.querySelector(`[name="${k}"]`);
      if(el && v !== null && v !== undefined) el.value = v;
    });
  }

  function renderKit(kit){
    if(!kitSummary) return;
    if(!kit || !kit.summary){
      kitSummary.hidden = true;
      kitSummary.textContent = "";
      return;
    }
    kitSummary.hidden = false;
    kitSummary.textContent = "Комплект документов:\n" + kit.summary;
  }

  function renderDocuments(documents){
    if(!documentsSection || !documentsList) return;
    if(!documents || !documents.length){
      documentsSection.hidden = true;
      documentsList.innerHTML = "";
      return;
    }
    documentsSection.hidden = false;
    documentsList.innerHTML = "";
    documents.forEach((doc)=>{
      const card = document.createElement("article");
      card.className = "document-card";
      card.dataset.fileId = String(doc.id);

      const title = document.createElement("div");
      title.className = "document-title";
      title.textContent = doc.original_filename || ("Файл #" + doc.id);

      const meta = document.createElement("div");
      meta.className = "document-meta";
      meta.innerHTML =
        "<div>Распознанный тип: <strong>" + escapeHtml(doc.detected_document_type_label || doc.detected_document_type || "—") + "</strong></div>" +
        "<div>Имя на Яндекс Диске: <strong>" + escapeHtml(doc.final_yadisk_name || "—") + "</strong></div>" +
        "<div>Статус: <strong>" + escapeHtml(doc.recognition_status || "—") + "</strong></div>";

      const label = document.createElement("label");
      label.className = "field";
      const span = document.createElement("span");
      span.textContent = "Тип документа";
      const select = document.createElement("select");
      select.className = "document-type-select";
      select.dataset.fileId = String(doc.id);
      DOCUMENT_TYPE_OPTIONS.forEach((option)=>{
        const opt = document.createElement("option");
        opt.value = option.value;
        opt.textContent = option.label;
        if(option.value === doc.detected_document_type) opt.selected = true;
        select.appendChild(opt);
      });
      select.disabled = alreadySaved;
      select.addEventListener("change", () => updateDocumentType(doc.id, select.value, select));
      label.appendChild(span);
      label.appendChild(select);

      card.appendChild(title);
      card.appendChild(meta);
      card.appendChild(label);
      documentsList.appendChild(card);
    });
  }

  function escapeHtml(value){
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setFormReadonly(readonly){
    Array.from(form.elements).forEach((el)=>{
      if(el === fallback) return;
      el.disabled = readonly;
    });
    if(fallback) fallback.disabled = readonly || submitting;
    if(isTelegram && tg && tg.MainButton){
      if(readonly){
        tg.MainButton.setText("Клиент уже сохранён");
        tg.MainButton.disable();
      } else {
        tg.MainButton.setText(formMode === "create_from_batch" ? "Сохранить клиента" : "Сохранить данные");
        tg.MainButton.enable();
      }
    }
  }

  async function updateDocumentType(fileId, documentType, selectEl){
    if(alreadySaved || !batchId) return;
    clearMessages();
    if(selectEl) selectEl.disabled = true;
    try{
      const res = await fetch(
        "/api/customer-batches/" + batchId + "/files/" + fileId + "/document-type",
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            document_type: documentType,
            context_token: getContextToken(),
            telegram_init_data: tg ? tg.initData || "" : "",
          }),
        }
      );
      const data = await res.json().catch(()=>({}));
      if(!res.ok || !data.ok){
        showError((data.error && data.error.message) || "Не удалось обновить тип документа");
        return;
      }
      if(data.file){
        window.__batchDocuments = replaceDocument(window.__batchDocuments || [], data.file);
        renderDocuments(window.__batchDocuments);
      }
      if(data.kit) renderKit(data.kit);
      showWarnings(data.warnings || []);
      if(data.values) fillValues(data.values);
      if(data.move_error){
        showError("Тип обновлён, но переименование на Яндекс Диске не удалось. Исходный файл сохранён.");
      }
    }catch(_e){
      showError("Ошибка сети при обновлении типа документа");
    }finally{
      if(selectEl && !alreadySaved) selectEl.disabled = false;
    }
  }

  function replaceDocument(list, file){
    const next = list.slice();
    const index = next.findIndex((item)=>Number(item.id) === Number(file.id));
    if(index >= 0) next[index] = file;
    else next.push(file);
    return next;
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
      formMode = data.mode || "edit";
      batchId = data.batch_id || null;
      alreadySaved = Boolean(data.already_saved);
      fillValues(data.values || {});
      showWarnings(data.warnings || []);
      if(formMode === "create_from_batch"){
        window.__batchDocuments = data.documents || [];
        renderDocuments(window.__batchDocuments);
        renderKit(data.kit);
        if (tg && tg.MainButton) tg.MainButton.setText("Сохранить клиента");
        if (fallback) fallback.textContent = "Сохранить клиента";
      }
      if(alreadySaved){
        showSuccess(data.message || "Клиент уже сохранён");
        setFormReadonly(true);
      }
    }catch(e){ showError("Ошибка сети при загрузке"); }
    finally{ setLoading(false); }
  }

  function setLoading(v) {
    submitting = v;
    if (fallback) fallback.disabled = v || alreadySaved;
    if (isTelegram && tg && tg.MainButton) {
      if (v) {
        tg.MainButton.showProgress();
        tg.MainButton.disable();
      } else {
        tg.MainButton.hideProgress();
        if(!alreadySaved) tg.MainButton.enable();
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
    if (isSubmitting || alreadySaved) return;
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
      if(data.already_saved || data.mode === "create_from_batch"){
        alreadySaved = true;
        setFormReadonly(true);
      }
      showSuccess(data.message || "Данные клиента сохранены");
      if(data.warnings && data.warnings.length){
        showWarnings(data.warnings);
      }
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
    try {
      tg.MainButton.onClick(submitCustomer);
    } catch (_e) {}
  }
  loadContext();
})();
