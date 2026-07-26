(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  const app = document.getElementById("app");
  const globalError = document.getElementById("global-error");

  const state = {
    maxFileBytes: 20 * 1024 * 1024,
    botUsername: null,
    batch: null,
    pollTimer: null,
    recognizing: false,
  };

  const SLOTS = [
    { type: "passport_main", title: "Паспорт — главная страница" },
    { type: "passport_registration", title: "Прописка" },
    { type: "snils", title: "СНИЛС" },
    { type: "tin", title: "ИНН" },
  ];

  const PREVIEW_LABELS = [
    ["last_name", "Фамилия"],
    ["first_name", "Имя"],
    ["surname", "Отчество"],
    ["passport", "Паспорт"],
    ["date_issue", "Дата выдачи"],
    ["department_code", "Код подразделения"],
    ["birth_date", "Дата рождения"],
    ["birth_place", "Место рождения"],
    ["registration_address", "Адрес регистрации"],
    ["ipain", "СНИЛС"],
    ["tin", "ИНН"],
  ];

  function initData() {
    return (tg && tg.initData) || "";
  }

  function pathName() {
    return window.location.pathname.replace(/\/+$/, "") || "/";
  }

  function navigate(path) {
    if (window.location.pathname !== path) {
      window.history.pushState({}, "", path);
    }
    render();
  }

  function showError(message) {
    if (!globalError) return;
    globalError.hidden = !message;
    globalError.textContent = message || "";
  }

  function escapeHtml(value) {
    return String(value || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(url, options) {
    const opts = options || {};
    const headers = Object.assign({}, opts.headers || {});
    if (!headers["Content-Type"] && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
    }
    if (initData()) {
      headers["X-Telegram-Init-Data"] = initData();
    }
    const response = await fetch(url, Object.assign({}, opts, { headers: headers }));
    const data = await response.json().catch(function () { return {}; });
    if (!response.ok || data.ok === false) {
      const err = (data && data.error) || {};
      const error = new Error(err.message || "Ошибка запроса");
      error.code = err.code;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function syncBackButton(isHome) {
    if (!tg || !tg.BackButton) return;
    if (isHome) {
      tg.BackButton.hide();
      return;
    }
    tg.BackButton.show();
  }

  function renderHome() {
    syncBackButton(true);
    app.innerHTML =
      '<div class="header"><h1>Arthur AutoExport</h1>' +
      '<p class="subtitle">Работа с клиентами и документами</p></div>' +
      '<div class="stack">' +
      '<button class="btn btn-primary btn-large" data-nav="/miniapp/customer/create">Добавить клиента</button>' +
      '<button class="btn btn-secondary btn-large" data-nav="/miniapp/customer/search">Найти клиента и сформировать документы</button>' +
      "</div>";
  }

  function slotFile(type) {
    const files = (state.batch && state.batch.files) || [];
    return files.find(function (item) { return item.declared_document_type === type; }) || null;
  }

  function renderCreate() {
    syncBackButton(false);
    const batch = state.batch;
    if (!batch) {
      app.innerHTML =
        '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
        '<div class="header"><h1>Создание клиента</h1>' +
        '<p class="subtitle">Загрузите документы клиента</p></div>' +
        '<div class="alert alert-info">Подготовка формы…</div>';
      return;
    }

    if (["recognizing", "recognized", "creating_folder", "uploading"].indexOf(batch.status) >= 0) {
      renderProcessing(batch);
      return;
    }

    if (["files_saved", "awaiting_confirmation"].indexOf(batch.status) >= 0) {
      renderPreview(batch);
      return;
    }

    if (batch.status === "customer_saved") {
      app.innerHTML =
        '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
        '<div class="alert alert-success">Клиент сохранён</div>' +
        '<div class="stack">' +
        (batch.customer_id
          ? '<button class="btn btn-primary" data-open-card="' + batch.customer_id + '">Открыть карточку клиента</button>' +
            '<button class="btn btn-secondary" data-open-spec="' + batch.customer_id + '">Добавить спецификацию</button>'
          : "") +
        '<button class="btn btn-secondary" data-nav="/miniapp">На главную</button>' +
        "</div>";
      return;
    }

    const slotsHtml = SLOTS.map(function (slot) {
      const file = slotFile(slot.type);
      const status = file ? "Файл выбран" : "Файл не выбран";
      const actions = file
        ? '<div class="filename">' + escapeHtml(file.original_filename || "файл") + "</div>" +
          '<div class="slot-actions">' +
          '<label class="btn btn-secondary">' +
          "Заменить файл" +
          '<input type="file" hidden accept="image/*,.pdf,application/pdf" data-slot="' + slot.type + '" />' +
          "</label>" +
          '<button class="btn btn-danger" data-remove-file="' + file.id + '">Удалить</button>' +
          "</div>"
        : '<label class="btn btn-secondary">' +
          "Выбрать файл" +
          '<input type="file" hidden accept="image/*,.pdf,application/pdf" capture="environment" data-slot="' + slot.type + '" />' +
          "</label>";
      return (
        '<section class="upload-slot" data-type="' + slot.type + '">' +
        "<h3>" + escapeHtml(slot.title) + "</h3>" +
        '<div class="status">' + status + "</div>" +
        actions +
        "</section>"
      );
    }).join("");

    const canRecognize = Boolean(batch.can_recognize);
    app.innerHTML =
      '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
      '<div class="header"><h1>Создание клиента</h1>' +
      '<p class="subtitle">Загрузите документы клиента</p></div>' +
      '<p class="hint">Можно загружать фото в любой ориентации — приложение попробует повернуть документ автоматически. Убедитесь, что весь документ попал в кадр и текст не размыт.</p>' +
      '<div class="stack">' + slotsHtml + "</div>" +
      '<p class="hint" style="margin-top:14px">Если загрузка из приложения не работает, документы можно отправить боту командой /add_customer.</p>' +
      '<div class="stack" style="margin-top:16px">' +
      '<button class="btn btn-primary" id="recognize-btn" ' + (canRecognize ? "" : "disabled") + ">Распознать документы</button>" +
      "</div>";
  }

  function renderProcessing(batch) {
    const steps = [
      { key: "upload", label: "Загрузка файлов", done: true },
      {
        key: "ocr",
        label: "Распознавание",
        active: batch.status === "recognizing",
        done: ["recognized", "creating_folder", "uploading", "files_saved"].indexOf(batch.status) >= 0,
      },
      {
        key: "kit",
        label: "Проверка комплекта",
        active: batch.status === "recognized",
        done: ["creating_folder", "uploading", "files_saved"].indexOf(batch.status) >= 0,
      },
      {
        key: "disk",
        label: "Сохранение на Яндекс Диск",
        active: ["creating_folder", "uploading"].indexOf(batch.status) >= 0,
        done: batch.status === "files_saved" || batch.status === "awaiting_confirmation",
      },
    ];
    app.innerHTML =
      '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
      '<div class="header"><h1>Документы обрабатываются</h1>' +
      '<p class="subtitle">' + escapeHtml(batch.user_message || "Обработка") + "</p></div>" +
      '<div class="loader">' +
      steps
        .map(function (step) {
          const cls = step.done ? "done" : step.active ? "active" : "";
          return '<div class="loader-step ' + cls + '">' + escapeHtml(step.label) + "</div>";
        })
        .join("") +
      "</div>" +
      '<p class="hint">Точное время обработки зависит от качества файлов и загрузки сервиса.</p>';
  }

  function renderPreview(batch) {
    const preview = batch.preview || {};
    const rows = PREVIEW_LABELS.map(function (pair) {
      const value = preview[pair[0]] || "не распознано";
      return (
        '<div class="preview-row"><div class="label">' +
        escapeHtml(pair[1]) +
        '</div><div class="value">' +
        escapeHtml(value) +
        "</div></div>"
      );
    }).join("");
    const kit = (batch.kit && batch.kit.summary) || batch.kit_message || "";
    const warnings = (batch.warnings || []).map(function (item) { return "• " + item; }).join("\n");
    app.innerHTML =
      '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
      '<div class="header"><h1>Распознанные данные клиента</h1></div>' +
      (kit ? '<div class="kit-box">' + escapeHtml(kit) + "</div>" : "") +
      (warnings ? '<div class="alert alert-warning">' + escapeHtml(warnings) + "</div>" : "") +
      '<div class="preview-list">' + rows + "</div>" +
      '<button class="btn btn-primary" id="open-customer-form">Проверить и сохранить клиента</button>';
  }

  function renderSearch(customer) {
    syncBackButton(false);
    if (customer) {
      const rows = (customer.fields || [])
        .map(function (field) {
          return (
            '<div class="card-row"><div class="label">' +
            escapeHtml(field.label) +
            '</div><div class="value">' +
            escapeHtml(field.value) +
            "</div></div>"
          );
        })
        .join("");
      app.innerHTML =
        '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
        '<div class="header"><h1>Карточка клиента</h1>' +
        '<p class="subtitle">' + escapeHtml(customer.fio) + "</p></div>" +
        '<div class="card-list">' + rows + "</div>" +
        '<div class="stack">' +
        '<button class="btn btn-primary" data-open-edit="' + customer.customer_id + '">Изменить данные</button>' +
        '<button class="btn btn-secondary" data-open-spec="' + customer.customer_id + '" ' +
        (customer.has_specification ? "disabled" : "") +
        ">Добавить спецификацию</button>" +
        '<button class="btn btn-secondary" id="new-search">← Новый поиск</button>' +
        "</div>";
      return;
    }

    app.innerHTML =
      '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
      '<div class="header"><h1>Карточка клиента</h1>' +
      '<p class="subtitle">Найдите клиента по номеру паспорта</p></div>' +
      '<label class="field"><span>Номер паспорта</span>' +
      '<input id="passport-input" inputmode="numeric" placeholder="80 11 541410" /></label>' +
      '<button class="btn btn-primary" id="search-btn">Поиск</button>' +
      '<div id="search-result"></div>';
  }

  function renderConflict(payload) {
    app.innerHTML =
      '<button class="back-link" data-nav="/miniapp">← Назад</button>' +
      '<div class="alert alert-warning">У вас уже есть незавершённое добавление клиента.</div>' +
      '<div class="stack">' +
      '<button class="btn btn-primary" id="continue-batch">Продолжить</button>' +
      '<button class="btn btn-danger" id="restart-batch">Отменить и начать заново</button>' +
      "</div>";
    state._conflict = payload;
  }

  async function ensureBootstrap() {
    if (!initData()) {
      showError("Откройте Mini App из Telegram.");
      return false;
    }
    const data = await api("/api/miniapp/bootstrap", {
      method: "POST",
      body: JSON.stringify({ telegram_init_data: initData() }),
    });
    state.maxFileBytes = data.max_file_bytes || state.maxFileBytes;
    state.botUsername = data.bot_username || null;
    return true;
  }

  async function loadActiveOrCreate(forceNew) {
    const created = await api("/api/customer-batches", {
      method: "POST",
      body: JSON.stringify({
        telegram_init_data: initData(),
        force_new: Boolean(forceNew),
      }),
    });
    if (created.conflict) {
      renderConflict(created);
      return;
    }
    state.batch = created.batch;
    renderCreate();
    if (["recognizing", "recognized", "creating_folder", "uploading"].indexOf(state.batch.status) >= 0) {
      startPolling();
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(async function () {
      try {
        if (!state.batch) return;
        const data = await api("/api/customer-batches/" + state.batch.batch_id + "/status", {
          method: "GET",
        });
        state.batch = data.batch;
        if (["files_saved", "awaiting_confirmation", "customer_saved", "failed", "abandoned"].indexOf(state.batch.status) >= 0) {
          stopPolling();
          state.recognizing = false;
        }
        if (pathName() === "/miniapp/customer/create") {
          renderCreate();
        }
      } catch (_error) {
        // Keep polling; transient network errors should not crash UI.
      }
    }, 2500);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function handleFileSelect(input) {
    const file = input.files && input.files[0];
    const slot = input.getAttribute("data-slot");
    input.value = "";
    if (!file || !slot || !state.batch) return;
    showError("");
    if (file.size > state.maxFileBytes) {
      const mb = Math.max(1, Math.floor(state.maxFileBytes / (1024 * 1024)));
      showError("Файл слишком большой. Максимум " + mb + " МБ.");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("declared_document_type", slot);
    form.append("telegram_init_data", initData());
    try {
      const data = await api("/api/customer-batches/" + state.batch.batch_id + "/files", {
        method: "POST",
        body: form,
        headers: {},
      });
      state.batch = data.batch;
      renderCreate();
    } catch (error) {
      showError(error.message || "Не удалось загрузить файл");
    }
  }

  async function openLaunch(url) {
    if (tg && typeof tg.openLink === "function") {
      // Stay inside Mini App navigation when possible.
    }
    window.location.href = url;
  }

  async function render() {
    showError("");
    const path = pathName();
    try {
      const ok = await ensureBootstrap();
      if (!ok) {
        app.innerHTML = '<div class="alert alert-error">Откройте Mini App из Telegram.</div>';
        return;
      }
      if (path === "/miniapp" || path === "") {
        stopPolling();
        renderHome();
        return;
      }
      if (path === "/miniapp/customer/create") {
        if (!state.batch) {
          renderCreate();
          await loadActiveOrCreate(false);
        } else {
          renderCreate();
        }
        return;
      }
      if (path === "/miniapp/customer/search") {
        stopPolling();
        let pendingId = null;
        try {
          pendingId = sessionStorage.getItem("miniapp_open_customer_id");
          if (pendingId) sessionStorage.removeItem("miniapp_open_customer_id");
        } catch (_e) {}
        if (pendingId) {
          try {
            const data = await api("/api/miniapp/customer-card", {
              method: "POST",
              body: JSON.stringify({
                telegram_init_data: initData(),
                customer_id: Number(pendingId),
              }),
            });
            state.customer = data.customer;
          } catch (error) {
            showError(error.message || "Клиент не найден");
          }
        }
        renderSearch(state.customer || null);
        return;
      }
      navigate("/miniapp");
    } catch (error) {
      showError(error.message || "Ошибка Telegram initData");
      app.innerHTML = '<div class="alert alert-error">' + escapeHtml(error.message || "Ошибка") + "</div>";
    }
  }

  document.addEventListener("click", async function (event) {
    const target = event.target.closest("[data-nav],button,label");
    if (!target) return;

    const nav = target.getAttribute("data-nav");
    if (nav) {
      event.preventDefault();
      state.customer = null;
      navigate(nav);
      return;
    }

    if (target.id === "recognize-btn") {
      if (!state.batch || state.recognizing) return;
      state.recognizing = true;
      try {
        const data = await api("/api/customer-batches/" + state.batch.batch_id + "/recognize", {
          method: "POST",
          body: JSON.stringify({ telegram_init_data: initData() }),
        });
        state.batch = data.batch;
        renderCreate();
        startPolling();
      } catch (error) {
        state.recognizing = false;
        showError(error.message || "Распознавание временно недоступно");
      }
      return;
    }

    if (target.id === "open-customer-form") {
      try {
        const data = await api("/api/miniapp/batch-form-token", {
          method: "POST",
          body: JSON.stringify({
            telegram_init_data: initData(),
            batch_id: state.batch.batch_id,
          }),
        });
        await openLaunch(data.url);
      } catch (error) {
        showError(error.message || "Форма устарела");
      }
      return;
    }

    if (target.id === "search-btn") {
      const input = document.getElementById("passport-input");
      const result = document.getElementById("search-result");
      const passport = (input && input.value) || "";
      try {
        const data = await api("/api/customers/search", {
          method: "POST",
          body: JSON.stringify({
            telegram_init_data: initData(),
            passport: passport,
          }),
        });
        if (data.status === "not_found") {
          result.innerHTML = '<div class="alert alert-warning">Клиент не найден</div>';
          return;
        }
        state.customer = data.customer;
        renderSearch(state.customer);
      } catch (error) {
        if (result) {
          result.innerHTML = '<div class="alert alert-error">' + escapeHtml(error.message) + "</div>";
        } else {
          showError(error.message);
        }
      }
      return;
    }

    if (target.id === "new-search") {
      state.customer = null;
      renderSearch(null);
      return;
    }

    if (target.id === "continue-batch") {
      try {
        const active = await api("/api/customer-batches/active", { method: "GET" });
        if (active.batch) {
          state.batch = active.batch;
          renderCreate();
          if (["recognizing", "recognized", "creating_folder", "uploading"].indexOf(state.batch.status) >= 0) {
            startPolling();
          }
          return;
        }
        showError("Завершите добавление в чате бота или нажмите «Отменить и начать заново».");
      } catch (error) {
        showError(error.message || "Не удалось продолжить");
      }
      return;
    }

    if (target.id === "restart-batch") {
      await loadActiveOrCreate(true);
      return;
    }

    const removeId = target.getAttribute("data-remove-file");
    if (removeId && state.batch) {
      try {
        const data = await api(
          "/api/customer-batches/" + state.batch.batch_id + "/files/" + removeId,
          {
            method: "DELETE",
            body: JSON.stringify({ telegram_init_data: initData() }),
          }
        );
        state.batch = data.batch;
        renderCreate();
      } catch (error) {
        showError(error.message || "Не удалось удалить файл");
      }
      return;
    }

    const editId = target.getAttribute("data-open-edit");
    if (editId) {
      try {
        const data = await api("/api/miniapp/customer-edit-token", {
          method: "POST",
          body: JSON.stringify({
            telegram_init_data: initData(),
            customer_id: Number(editId),
          }),
        });
        await openLaunch(data.url);
      } catch (error) {
        showError(error.message || "Нет доступа");
      }
      return;
    }

    const cardId = target.getAttribute("data-open-card");
    if (cardId) {
      try {
        sessionStorage.setItem("miniapp_open_customer_id", String(cardId));
      } catch (_e) {}
      navigate("/miniapp/customer/search");
      return;
    }

    const specId = target.getAttribute("data-open-spec");
    if (specId) {
      try {
        const data = await api("/api/miniapp/specification-token", {
          method: "POST",
          body: JSON.stringify({
            telegram_init_data: initData(),
            customer_id: Number(specId),
          }),
        });
        await openLaunch(data.url);
      } catch (error) {
        showError(error.message || "Не удалось открыть спецификацию");
      }
    }
  });

  document.addEventListener("change", function (event) {
    const input = event.target;
    if (input && input.matches('input[type="file"][data-slot]')) {
      handleFileSelect(input);
    }
  });

  window.addEventListener("popstate", function () {
    render();
  });

  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.BackButton) {
      tg.BackButton.onClick(function () {
        const path = pathName();
        if (path === "/miniapp/customer/create" || path === "/miniapp/customer/search") {
          navigate("/miniapp");
          return;
        }
        if (window.history.length > 1) {
          window.history.back();
        } else {
          navigate("/miniapp");
        }
      });
    }
  }

  render();
})();
