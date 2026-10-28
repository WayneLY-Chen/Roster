// Gmail 標籤頁。每一段都是「預覽 → 勾選 → 套用」。
// 表格內容一律用 textContent 放進去：公司名稱是爬來的資料，不能當成 HTML。
(function () {
  const labelSelect = document.getElementById("label");
  const limitInput = document.getElementById("limit");
  const loadButton = document.getElementById("load-labels");
  const labelsMessage = document.getElementById("labels-message");

  async function request(url, init) {
    const response = await fetch(url, init);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "發生錯誤（" + response.status + "）");
    return data;
  }

  const get = (url) => request(url);
  const post = (url, body) =>
    request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  function say(el, text, kind) {
    el.textContent = text;
    el.className = "notice" + (kind ? " notice-" + kind : "");
    el.hidden = !text;
  }

  // 按鈕在等待時鎖住，並顯示「處理中」；結束時恢復原本的字。
  async function busy(button, work) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "處理中…";
    try {
      return await work();
    } finally {
      button.textContent = original;
      button.disabled = false;
    }
  }

  function cell(text, className) {
    const td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    return td;
  }

  // ------------------------------------------------------------ 標籤清單

  loadButton.addEventListener("click", () =>
    busy(loadButton, async () => {
      say(labelsMessage, "");
      try {
        const data = await get("/api/gmail/labels");
        labelSelect.replaceChildren(
          ...data.labels.map((name) => {
            const option = document.createElement("option");
            option.value = option.textContent = name;
            return option;
          })
        );
        const has = data.labels.length > 0;
        labelSelect.disabled = !has;
        document
          .querySelectorAll('[data-panel="classify"] [data-action="scan"], [data-panel="harvest"] [data-action="scan"]')
          .forEach((button) => (button.disabled = !has));
        say(labelsMessage, has ? "讀到 " + data.labels.length + " 個標籤。" : "你的 Gmail 還沒有自己建的標籤。", has ? "success" : "");
      } catch (error) {
        say(labelsMessage, error.message, "error");
      }
    })
  );

  // ------------------------------------------------------------ 三段共用

  function panel(name, options) {
    const root = document.querySelector('[data-panel="' + name + '"]');
    const scanButton = root.querySelector('[data-action="scan"]');
    const applyButton = root.querySelector('[data-action="apply"]');
    const message = root.querySelector('[data-role="message"]');
    const wrap = root.querySelector(".table-wrap");
    const tbody = root.querySelector("tbody");
    const all = root.querySelector('[data-role="all"]');

    if (all) {
      all.addEventListener("change", () =>
        tbody.querySelectorAll("input[type=checkbox]:not(:disabled)").forEach((box) => (box.checked = all.checked))
      );
    }

    function render(rows) {
      tbody.replaceChildren(
        ...rows.map((row, index) => {
          const tr = document.createElement("tr");
          if (options.checkable) {
            const td = document.createElement("td");
            td.className = "check";
            const box = document.createElement("input");
            box.type = "checkbox";
            box.value = index;
            box.checked = options.checkedByDefault(row);
            box.disabled = !options.selectable(row);
            td.appendChild(box);
            tr.appendChild(td);
          }
          options.cells(row).forEach((td) => tr.appendChild(td));
          return tr;
        })
      );
      wrap.hidden = rows.length === 0;
    }

    function checkedIndices() {
      return [...tbody.querySelectorAll("input[type=checkbox]:checked")].map((box) => Number(box.value));
    }

    scanButton.addEventListener("click", () =>
      busy(scanButton, async () => {
        say(message, "");
        applyButton.disabled = true;
        try {
          const data = await post(options.scanUrl, options.scanBody());
          render(data.rows);
          say(message, data.summary, data.rows.length ? "" : "success");
          applyButton.disabled = data.rows.length === 0;
        } catch (error) {
          render([]);
          say(message, error.message, "error");
        }
      })
    );

    applyButton.addEventListener("click", () => {
      if (options.confirmText && !confirm(options.confirmText())) return;
      busy(applyButton, async () => {
        try {
          const data = await post(options.applyUrl, options.applyBody(checkedIndices()));
          render([]);
          say(message, data.summary, "success");
        } catch (error) {
          say(message, error.message, "error");
        }
      }).then(() => (applyButton.disabled = true));
    });
  }

  const scanBody = () => ({ label: labelSelect.value, limit: Number(limitInput.value) || 200 });

  panel("classify", {
    scanUrl: "/api/gmail/classify/scan",
    applyUrl: "/api/gmail/classify/apply",
    scanBody,
    applyBody: (indices) => ({ indices }),
    checkable: true,
    checkedByDefault: (row) => !row.already,
    selectable: (row) => !row.already,
    cells: (row) => [cell(row.company_name, "company"), cell(row.address, "mono"), cell(row.already ? "已經有這個標籤" : "會貼上", "muted")],
  });

  panel("harvest", {
    scanUrl: "/api/gmail/harvest/scan",
    applyUrl: "/api/gmail/harvest/apply",
    scanBody,
    applyBody: (indices) => ({ indices }),
    checkable: true,
    checkedByDefault: (row) => row.storable,
    selectable: (row) => row.storable,
    cells: (row) => [cell(row.company_name, "company"), cell(row.person || "—"), cell(row.email || "—", "mono"), cell(row.action, "muted")],
  });

  const syncBody = (apply) => ({
    tags: document.getElementById("sync-tags").checked,
    status: document.getElementById("sync-status").checked,
    apply,
  });

  panel("sync", {
    scanUrl: "/api/gmail/sync",
    applyUrl: "/api/gmail/sync",
    scanBody: () => syncBody(false),
    applyBody: () => syncBody(true),
    checkable: false,
    confirmText: () => "要把預覽裡的標籤貼到你的 Gmail 嗎？只會新增標籤，不會刪信或改已讀。",
    cells: (row) => [cell(row.label), cell(String(row.companies), "num"), cell(String(row.messages), "num")],
  });
})();
