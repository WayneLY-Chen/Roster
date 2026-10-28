// 爬取頁：按下開始後每秒問一次進度，跑完重新整理頁面讓「最近幾次」更新。
(function () {
  const start = document.getElementById("start");
  if (!start) return;

  const cancel = document.getElementById("cancel");
  const source = document.getElementById("source");
  const progress = document.getElementById("progress");
  const fill = document.getElementById("progress-fill");
  const text = document.getElementById("progress-text");
  const message = document.getElementById("message");
  let timer = null;

  function show(el, visible) { el.hidden = !visible; }

  function say(content, kind) {
    message.textContent = content;
    message.className = "notice" + (kind ? " notice-" + kind : "");
    show(message, Boolean(content));
  }

  function render(state) {
    start.disabled = state.running;
    if (!state.running) cancel.disabled = false;
    show(cancel, state.running);
    show(progress, state.running);

    const p = state.progress;
    if (state.running && p) {
      const ratio = p.total ? Math.min(p.page / p.total, 1) : 0;
      fill.style.width = (ratio * 100).toFixed(0) + "%";
      text.textContent = p.source + "：第 " + p.page + (p.total ? " / " + p.total : "") +
        " 頁，已存 " + p.stored + " 家";
    } else if (state.running) {
      fill.style.width = "0%";
      text.textContent = "準備中…";
    }
  }

  async function poll() {
    const state = await (await fetch("/api/crawl")).json();
    render(state);
    if (state.running) return;

    clearInterval(timer);
    timer = null;
    if (state.error) {
      say("爬取失敗：" + state.error, "error");
    } else if (state.results.length) {
      const added = state.results.reduce((sum, r) => sum + r.records_new, 0);
      say("完成，新增 " + added + " 家。", "success");
      setTimeout(() => location.reload(), 1500);
    }
  }

  function follow() {
    if (!timer) timer = setInterval(poll, 1000);
  }

  start.addEventListener("click", async () => {
    say("");
    const response = await fetch("/api/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: source.value || null }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      say(body.detail || "沒辦法開始爬取。", "error");
      return;
    }
    render(await response.json());
    follow();
  });

  cancel.addEventListener("click", async () => {
    cancel.disabled = true;
    await fetch("/api/crawl/cancel", { method: "POST" });
    text.textContent = "取消中，等這一頁處理完…";
  });

  // 重新整理頁面時，背景的爬取可能還在跑；在跑才接著問，沒在跑就不打擾。
  fetch("/api/crawl")
    .then((response) => response.json())
    .then((state) => {
      render(state);
      if (state.running) follow();
    });
})();
