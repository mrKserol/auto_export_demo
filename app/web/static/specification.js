(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const form = document.getElementById("specification-form");
  const formError = document.getElementById("form-error");
  const formSuccess = document.getElementById("form-success");
  const fallbackSubmit = document.getElementById("fallback-submit");

  let submitting = false;

  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.MainButton) {
      document.body.classList.add("has-main-button");
      tg.MainButton.setText("Сохранить спецификацию");
      tg.MainButton.show();
      tg.MainButton.onClick(() => {
        form.requestSubmit();
      });
    }
  }

  // detect edit mode and prefill via API
  const urlParams = new URLSearchParams(window.location.search);
  const mode = urlParams.get("mode");
  const contextToken = getContextToken();
  async function loadEditContext() {
    if (mode !== "edit") return;
    // change UI texts
    const title = document.querySelector(".header h1");
    const subtitle = document.querySelector(".header .subtitle");
    if (title) title.textContent = "Изменить спецификацию";
    if (subtitle) subtitle.textContent = "Проверьте данные автомобиля и внесите изменения";
    if (tg && tg.MainButton) tg.MainButton.setText("Сохранить изменения");

    setLoading(true);
    try {
      const response = await fetch("/api/specifications/edit-context", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          context_token: contextToken,
          telegram_init_data: tg ? tg.initData || "" : "",
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        showFormError(data.error?.message || "Не удалось загрузить спецификацию для редактирования");
        return;
      }
      const values = data.values || {};
      // fill inputs
      Object.entries(values).forEach(([k, v]) => {
        const input = form.querySelector(`[name="${k}"]`);
        if (input && v !== null && v !== undefined) input.value = v;
      });
    } catch (e) {
      showFormError("Ошибка сети при загрузке спецификации.");
    } finally {
      setLoading(false);
    }
  }
  loadEditContext();

  function getContextToken() {
    const params = new URLSearchParams(window.location.search);
    return (params.get("token") || "").trim();
  }

  function clearErrors() {
    formError.hidden = true;
    formError.textContent = "";
    formSuccess.hidden = true;
    formSuccess.textContent = "";
    form.querySelectorAll(".field").forEach((field) => field.classList.remove("has-error"));
    form.querySelectorAll(".field-error").forEach((node) => {
      node.textContent = "";
    });
  }

  function showFormError(message) {
    formError.hidden = false;
    formError.textContent = message;
  }

  function showFieldError(name, message) {
    const errorNode = form.querySelector(`[data-error-for="${name}"]`);
    const field = errorNode && errorNode.closest(".field");
    if (errorNode) {
      errorNode.textContent = message;
    }
    if (field) {
      field.classList.add("has-error");
    }
  }

  function haptic(type) {
    try {
      if (tg && tg.HapticFeedback) {
        tg.HapticFeedback.notificationOccurred(type);
      }
    } catch (_error) {
      // ignore unsupported haptic feedback
    }
  }

  function setLoading(isLoading) {
    submitting = isLoading;
    fallbackSubmit.disabled = isLoading;
    if (tg && tg.MainButton) {
      if (isLoading) {
        tg.MainButton.showProgress();
        tg.MainButton.disable();
      } else {
        tg.MainButton.hideProgress();
        tg.MainButton.enable();
      }
    }
  }

  function clientValidate(data) {
    const errors = {};
    const year = Number(data.year);
    const currentMaxYear = new Date().getFullYear() + 1;
    if (!data.brand) errors.brand = "Укажите марку";
    if (!data.model) errors.model = "Укажите модель";
    if (!Number.isInteger(year) || year < 1950 || year > currentMaxYear) {
      errors.year = `Год должен быть от 1950 до ${currentMaxYear}`;
    }
    const engCapacity = Number(data.eng_capacity);
    if (!(engCapacity >= 0.1 && engCapacity <= 20)) {
      errors.eng_capacity = "Объём двигателя: 0.1–20.0";
    }
    if (!data.eng_type) errors.eng_type = "Выберите тип двигателя";
    if (!data.drive) errors.drive = "Выберите привод";
    if (!data.transmission) errors.transmission = "Выберите КПП";
    if (!data.color) errors.color = "Укажите цвет";
    const mileage = Number(data.mileage);
    if (!Number.isInteger(mileage) || mileage < 0 || mileage > 5000000) {
      errors.mileage = "Пробег: 0–5 000 000";
    }
    const price = Number(data.price);
    if (!(price > 0 && price <= 1000000000)) {
      errors.price = "Стоимость должна быть больше 0";
    }
    return errors;
  }

  function collectPayload() {
    const formData = new FormData(form);
    return {
      brand: String(formData.get("brand") || "").trim(),
      model: String(formData.get("model") || "").trim(),
      year: Number(formData.get("year")),
      eng_capacity: String(formData.get("eng_capacity") || "").trim(),
      eng_type: String(formData.get("eng_type") || "").trim(),
      drive: String(formData.get("drive") || "").trim(),
      transmission: String(formData.get("transmission") || "").trim(),
      color: String(formData.get("color") || "").trim(),
      complectation: String(formData.get("complectation") || "").trim() || null,
      mileage: Number(formData.get("mileage")),
      price: String(formData.get("price") || "").trim(),
      currency: "CNY",
      context_token: getContextToken(),
      telegram_init_data: tg ? tg.initData || "" : "",
    };
  }

  async function submitForm() {
    if (submitting) return;
    clearErrors();

    const payload = collectPayload();
    if (!payload.telegram_init_data) {
      showFormError("Откройте форму через кнопку в Telegram-боте.");
      haptic("error");
      return;
    }
    if (!payload.context_token) {
      showFormError("Ссылка на форму недействительна. Откройте её заново из бота.");
      haptic("error");
      return;
    }

    const clientErrors = clientValidate(payload);
    if (Object.keys(clientErrors).length) {
      Object.entries(clientErrors).forEach(([name, message]) => showFieldError(name, message));
      showFormError("Проверьте заполнение формы");
      haptic("error");
      return;
    }

    setLoading(true);
    try {
      const endpoint = mode === "edit" ? "/api/specifications" : "/api/specifications";
      const method = mode === "edit" ? "PUT" : "POST";
      const response = await fetch(endpoint, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok || !data.ok) {
        const error = (data && data.error) || {};
        if (error.code === "SPECIFICATION_ALREADY_EXISTS") {
          showFormError(
            "У клиента уже есть спецификация. Откройте карточку клиента и выберите редактирование."
          );
        } else if (error.fields) {
          Object.entries(error.fields).forEach(([name, message]) => {
            const fieldName = String(name).split(".").pop();
            showFieldError(fieldName, message);
          });
          showFormError(error.message || "Проверьте заполнение формы");
        } else {
          showFormError(error.message || "Не удалось сохранить спецификацию");
        }
        haptic("error");
        return;
      }

      formSuccess.hidden = false;
      // show message and updated data returned from backend
      const messageLines = [];
      messageLines.push(data.message || "Спецификация сохранена");
      if (data.extra_message) messageLines.push(data.extra_message);
      if (data.specification_text) {
        messageLines.push("");
        messageLines.push("Обновлённая спецификация:");
        messageLines.push(data.specification_text);
      }
      if (data.customer_card) {
        messageLines.push("");
        messageLines.push("Карта клиента:");
        messageLines.push(data.customer_card);
      }
      formSuccess.textContent = messageLines.join("\n");
      haptic("success");
      // give user longer time to read before closing
      setTimeout(() => {
        if (tg && typeof tg.close === "function") {
          tg.close();
        }
      }, 2200);
    } catch (_error) {
      showFormError("Сеть недоступна. Попробуйте ещё раз.");
      haptic("error");
    } finally {
      setLoading(false);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitForm();
  });
})();
