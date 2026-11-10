// 샘플 할 일 시스템의 업무 화면(works/todo). 등록(index), 찾기(find), 상세(detail)를 함께 맡는다.
// 남길 메시지의 모양은 이 화면이 정하고, 남길지는 사용자가 [남기고 닫기]로 정한다.
(() => {
  "use strict";
  const Baro = window.Baro;
  const ctx = Baro.context;

  function fieldsOf(todo) {
    return [
      { label: "상태", value: todo.status_label },
      { label: "우선순위", value: todo.priority },
      { label: "마감일", value: todo.due },
      { label: "담당자", value: todo.assignee },
    ];
  }

  function showFields(list, rows) {
    list.replaceChildren();
    rows.forEach(({ label, value }) => {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      const data = document.createElement("dd");
      term.textContent = label;
      data.textContent = value || "-";
      row.append(term, data);
      list.append(row);
    });
  }

  function option(select, value, label) {
    const item = document.createElement("option");
    item.value = value;
    item.textContent = label;
    select.append(item);
    return item;
  }

  async function loadOptions() {
    const result = await Baro.api("GET", "options");
    if (!result.ok) Baro.status(Baro.errorText(result, "선택 목록을 불러오지 못했습니다."), { error: true });
    return result.ok ? result.data : { priorities: [], statuses: [] };
  }

  function showErrors(form, errors) {
    form.querySelectorAll("[data-error-for]").forEach((box) => {
      const message = errors[box.dataset.errorFor] || "";
      box.textContent = message;
      const input = form.elements[box.dataset.errorFor];
      if (input) input.setAttribute("aria-invalid", message ? "true" : "false");
    });
  }

  // 담당자: 바로채널 사용자 중에서 고른다. 고르지 않고 직접 써도 된다.
  function setupPeople(input, list) {
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const rows = await Baro.people(input.value);
        list.replaceChildren();
        rows.forEach((row) => { option(list, row.name, row.email); });
      }, 300);
    });
  }

  async function setupCreate() {
    const form = document.getElementById("todo-form");
    const priority = form.elements.priority;
    setupPeople(form.elements.assignee, document.getElementById("people"));
    const options = await loadOptions();
    priority.replaceChildren();
    options.priorities.forEach((value) => { option(priority, value, value).selected = value === "보통"; });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = document.getElementById("todo-submit");
      const data = Object.fromEntries(new FormData(form));
      const local = {};
      if (!data.title.trim()) local.title = "제목을 입력해 주세요.";
      showErrors(form, local);
      if (Object.keys(local).length) return;
      button.disabled = true;
      Baro.status("등록하는 중입니다…");
      // 같은 팝업에서 다시 보내도 할 일 시스템이 같은 결과를 돌려준다.
      const result = await Baro.api("POST", "", {
        body: { ...data, channel: ctx.channel.name },
        idempotent: true,
      });
      button.disabled = false;
      if (result.status === 422 && result.data && result.data.errors) {
        showErrors(form, result.data.errors);
        Baro.status("입력값을 확인해 주세요.", { error: true });
        return;
      }
      if (!result.ok) {
        Baro.status(Baro.errorText(result, "등록하지 못했습니다."), { error: true });
        return;
      }
      const todo = result.data;
      form.hidden = true;
      const section = document.getElementById("todo-result");
      showFields(section.querySelector("[data-todo-fields]"), [{ label: "제목", value: todo.title }, ...fieldsOf(todo)]);
      section.hidden = false;
      Baro.status("");
      Baro.message({
        text: `할 일을 등록했습니다: ${todo.title}`,
        fields: fieldsOf(todo).filter((row) => row.value),
        detail: { id: todo.id },
      });
    });
  }

  async function setupFind() {
    const form = document.getElementById("todo-search");
    const options = await loadOptions();
    options.statuses.forEach((row) => { option(form.elements.status, row.value, row.label); });
    const section = document.getElementById("todo-results");
    const list = section.querySelector("[data-todo-list]");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      Baro.message(null);
      Baro.status("찾는 중입니다…");
      const result = await Baro.api("GET", "", { query: Object.fromEntries(new FormData(form)) });
      if (!result.ok) {
        Baro.status(Baro.errorText(result, "찾지 못했습니다."), { error: true });
        return;
      }
      const { items, total } = result.data;
      list.replaceChildren();
      items.forEach((todo) => {
        const item = document.createElement("li");
        const button = document.createElement("button");
        const title = document.createElement("strong");
        const meta = document.createElement("small");
        button.type = "button";
        button.setAttribute("aria-pressed", "false");
        title.textContent = todo.title;
        meta.textContent = fieldsOf(todo).filter((row) => row.value).map((row) => `${row.label} ${row.value}`).join(" · ");
        button.append(title, meta);
        button.addEventListener("click", () => {
          list.querySelectorAll("button").forEach((other) => other.setAttribute("aria-pressed", "false"));
          button.setAttribute("aria-pressed", "true");
          Baro.message({ text: todo.title, fields: fieldsOf(todo).filter((row) => row.value), detail: { id: todo.id } });
          Baro.status("고른 할 일을 [남기고 닫기]로 채널에 공유할 수 있습니다.");
        });
        item.append(button);
        list.append(item);
      });
      section.querySelector("[data-todo-total]").textContent =
        total > items.length ? `${total}건 중 최근 ${items.length}건` : `${total}건`;
      section.hidden = false;
      Baro.status(items.length ? "공유할 할 일을 고르세요." : "찾은 할 일이 없습니다.");
    });
  }

  async function setupDetail() {
    const id = ctx.params.id;
    const section = document.getElementById("todo-detail");
    const buttons = section.querySelector("[data-todo-actions]");
    let first = null;

    function render(todo) {
      showFields(section.querySelector("[data-todo-fields]"), [
        { label: "제목", value: todo.title },
        { label: "설명", value: todo.description },
        ...fieldsOf(todo),
        { label: "등록자", value: todo.created_by },
        { label: "마지막 수정", value: todo.updated_by },
        { label: "등록 시각", value: todo.created_at },
        { label: "수정 시각", value: todo.updated_at },
      ]);
      buttons.replaceChildren();
      if (!ctx.can_write) return;
      todo.actions.forEach((action) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = action.label;
        button.addEventListener("click", async () => {
          if (action.confirm && !confirm(action.confirm)) return;
          buttons.querySelectorAll("button").forEach((other) => { other.disabled = true; });
          const result = await Baro.api("POST", `${encodeURIComponent(id)}/actions/${encodeURIComponent(action.key)}`);
          if (!result.ok) {
            Baro.status(Baro.errorText(result, "처리하지 못했습니다."), { error: true });
            buttons.querySelectorAll("button").forEach((other) => { other.disabled = false; });
            return;
          }
          render(result.data);
          Baro.status(`${action.label} 처리했습니다. [남기고 닫기]를 누르면 쓰레드에 남습니다.`);
          Baro.message({
            text: `${result.data.title}: ${action.label} 처리했습니다.`,
            fields: [{ label: "상태", value: `${first.status_label} → ${result.data.status_label}` }],
          });
        });
        buttons.append(button);
      });
    }

    const result = await Baro.api("GET", encodeURIComponent(id));
    document.getElementById("todo-loading").hidden = true;
    if (!result.ok) {
      Baro.status(Baro.errorText(result, "할 일을 불러오지 못했습니다."), { error: true });
      return;
    }
    first = result.data;
    render(first);
    section.hidden = false;
  }

  document.addEventListener("DOMContentLoaded", () => {
    const page = document.body.dataset.page;
    if (page === "index") setupCreate();
    else if (page === "find") setupFind();
    else if (page === "detail") setupDetail();
  });
})();
