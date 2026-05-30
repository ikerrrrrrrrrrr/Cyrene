// Calendar view for Plans page — month grid with task indicators and day popover
const { useState: useStateC } = React;

/* ── Helpers ─────────────────────────────────────────────────────────── */

function buildTasksByDate(tasks) {
  var map = {};
  tasks.forEach(function (task) {
    if (!task.next_run) return;
    var d = new Date(task.next_run);
    if (isNaN(d.getTime())) return;
    var key = d.getFullYear() + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" +
      String(d.getDate()).padStart(2, "0");
    if (!map[key]) map[key] = [];
    map[key].push({
      id: task.id,
      prompt: task.prompt,
      schedule_type: task.schedule_type,
      isRecurring: task.schedule_type === "cron" || task.schedule_type === "interval",
      status: task.status,
      next_run: task.next_run,
    });
  });
  return map;
}

function todayKey() {
  var d = new Date();
  return d.getFullYear() + "-" +
    String(d.getMonth() + 1).padStart(2, "0") + "-" +
    String(d.getDate()).padStart(2, "0");
}

/* Build 42-cell month grid starting on Monday.
   Each cell: { day: number, isOther: bool, dateKey: "YYYY-MM-DD" } */
function buildMonthGrid(year, month) {
  var first = new Date(year, month, 1);
  var lastDay = new Date(year, month + 1, 0).getDate();
  var startDow = first.getDay(); // 0=Sun
  var pad = startDow === 0 ? 6 : startDow - 1; // Mon-start offset
  var cells = [];
  var prevMonthLast = new Date(year, month, 0).getDate();

  // Pre-padding from previous month
  for (var i = pad - 1; i >= 0; i--) {
    var d = prevMonthLast - i;
    var m = month === 0 ? 11 : month - 1;
    var y = month === 0 ? year - 1 : year;
    cells.push({
      day: d,
      isOther: true,
      dateKey: y + "-" + String(m + 1).padStart(2, "0") + "-" + String(d).padStart(2, "0"),
    });
  }
  // Current month days
  for (var d2 = 1; d2 <= lastDay; d2++) {
    cells.push({
      day: d2,
      isOther: false,
      dateKey: year + "-" + String(month + 1).padStart(2, "0") + "-" + String(d2).padStart(2, "0"),
    });
  }
  // Post-padding from next month
  var total = cells.length;
  var remaining = 42 - total;
  for (var j = 1; j <= remaining; j++) {
    var m2 = month === 11 ? 0 : month + 1;
    var y2 = month === 11 ? year + 1 : year;
    cells.push({
      day: j,
      isOther: true,
      dateKey: y2 + "-" + String(m2 + 1).padStart(2, "0") + "-" + String(j).padStart(2, "0"),
    });
  }
  return cells;
}

function formatTime(isoStr) {
  try {
    var d = new Date(isoStr);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

var SCHEDULE_LABELS_C = { cron: "Cron", interval: "Interval", once: "Once" };

function scheduleLabel(task) {
  return SCHEDULE_LABELS_C[task.schedule_type] || task.schedule_type;
}

/* ── CalendarView ────────────────────────────────────────────────────── */

function CalendarView({ tasks, onEdit, onToggle, onDelete }) {
  var now = new Date();
  var [year, setYear] = useStateC(now.getFullYear());
  var [month, setMonth] = useStateC(now.getMonth());
  var [selectedKey, setSelectedKey] = useStateC(null);

  var tasksByDate = buildTasksByDate(tasks);
  var todayK = todayKey();
  var cells = buildMonthGrid(year, month);

  function goPrev() {
    if (month === 0) { setYear(year - 1); setMonth(11); }
    else { setMonth(month - 1); }
    setSelectedKey(null);
  }
  function goNext() {
    if (month === 11) { setYear(year + 1); setMonth(0); }
    else { setMonth(month + 1); }
    setSelectedKey(null);
  }
  function goToday() {
    var d = new Date();
    setYear(d.getFullYear());
    setMonth(d.getMonth());
    setSelectedKey(null);
  }

  var selectedTasks = selectedKey ? (tasksByDate[selectedKey] || []) : [];

  return React.createElement("div", { className: "tasks-calendar" },
    React.createElement(CalendarHeader, {
      year: year,
      month: month,
      onPrev: goPrev,
      onNext: goNext,
      onToday: goToday,
      onPickMonth: function (y, m) { setYear(y); setMonth(m); },
    }),
    React.createElement(CalendarGrid, {
      cells: cells,
      tasksByDate: tasksByDate,
      todayKey: todayK,
      selectedKey: selectedKey,
      onSelectDay: setSelectedKey,
    }),
    selectedKey && React.createElement(DayPopover, {
      dateKey: selectedKey,
      tasks: selectedTasks,
      onClose: function () { setSelectedKey(null); },
      onEdit: onEdit,
      onToggle: onToggle,
      onDelete: onDelete,
    })
  );
}

/* ── CalendarHeader + MonthPicker ────────────────────────────────────── */

function CalendarHeader({ year, month, onPrev, onNext, onToday, onPickMonth }) {
  var { t } = window.useI18n();
  var [pickerOpen, setPickerOpen] = useStateC(false);
  var monthName = new Date(year, month).toLocaleDateString(
    window.__i18nLang === "zh" ? "zh-CN" : "en-US",
    { month: "long" }
  );
  return React.createElement("div", { className: "tasks-cal-header" },
    React.createElement("div", { className: "tasks-cal-nav" },
      React.createElement("button", {
        className: "tasks-cal-nav-btn",
        onClick: onPrev,
        title: "Previous month",
      }, "‹"),
      React.createElement("button", {
        className: "tasks-cal-title-btn",
        onClick: function () { setPickerOpen(true); },
        title: "Select month",
      },
        React.createElement("span", { className: "tasks-cal-title" },
          monthName + " " + year
        )
      ),
      React.createElement("button", {
        className: "tasks-cal-nav-btn",
        onClick: onNext,
        title: "Next month",
      }, "›"),
    ),
    React.createElement("button", {
      className: "tasks-cal-today-btn",
      onClick: onToday,
    }, t("tasks.calendar.today")),
    pickerOpen && React.createElement(MonthPicker, {
      year: year,
      month: month,
      onPick: function (y, m) { setPickerOpen(false); onPickMonth(y, m); },
      onClose: function () { setPickerOpen(false); },
    })
  );
}

function MonthPicker({ year, month, onPick, onClose }) {
  var [pickerYear, setPickerYear] = useStateC(year);
  var lang = window.__i18nLang === "zh" ? "zh-CN" : "en-US";
  // Build short month names array
  var monthNames = [];
  for (var i = 0; i < 12; i++) {
    var d = new Date(2025, i, 1);
    monthNames.push(d.toLocaleDateString(lang, { month: "short" }));
  }

  return React.createElement("div", {
    className: "tasks-cal-picker-overlay",
    onClick: function (e) { if (e.target === e.currentTarget) onClose(); }
  },
    React.createElement("div", { className: "tasks-cal-picker", onClick: function (e) { e.stopPropagation(); } },
      React.createElement("div", { className: "tasks-cal-picker-year" },
        React.createElement("button", {
          className: "tasks-cal-picker-year-btn",
          onClick: function () { setPickerYear(pickerYear - 10); },
          title: "-10 years"
        }, "«"),
        React.createElement("button", {
          className: "tasks-cal-picker-year-btn",
          onClick: function () { setPickerYear(pickerYear - 1); },
        }, "‹"),
        React.createElement("span", {
          style: { cursor: "pointer", padding: "0 4px" },
          onClick: function () { onPick(pickerYear, month); },
          title: "Go to this year"
        }, pickerYear),
        React.createElement("button", {
          className: "tasks-cal-picker-year-btn",
          onClick: function () { setPickerYear(pickerYear + 1); },
        }, "›"),
        React.createElement("button", {
          className: "tasks-cal-picker-year-btn",
          onClick: function () { setPickerYear(pickerYear + 10); },
          title: "+10 years"
        }, "»"),
      ),
      React.createElement("div", { className: "tasks-cal-picker-months" },
        monthNames.map(function (name, i) {
          var cls = "tasks-cal-picker-month";
          if (pickerYear === year && i === month) cls += " tasks-cal-picker-month--current";
          return React.createElement("button", {
            key: i,
            className: cls,
            onClick: function () { onPick(pickerYear, i); },
          }, name);
        })
      )
    )
  );
}

/* ── CalendarGrid ────────────────────────────────────────────────────── */

function CalendarGrid({ cells, tasksByDate, todayKey, selectedKey, onSelectDay }) {
  var { t } = window.useI18n();
  var dayNames = [];
  var lang = window.__i18nLang === "zh" ? "zh-CN" : "en-US";
  // Monday-first: indices 1-6 = Tue-Sat, index 0 = Mon, index 6 catch-all
  for (var i = 0; i < 7; i++) {
    var d = new Date(2025, 0, 6 + i); // 2025-01-06 was a Monday
    dayNames.push(d.toLocaleDateString(lang, { weekday: "short" }));
  }

  return React.createElement("div", { className: "tasks-cal-grid" },
    // Column headers
    dayNames.map(function (name, i) {
      return React.createElement("div", { key: "hdr-" + i, className: "tasks-cal-day-header" }, name);
    }),
    // Day cells
    cells.map(function (cell, i) {
      var dayTasks = tasksByDate[cell.dateKey] || [];
      var isToday = cell.dateKey === todayKey;
      var isSelected = cell.dateKey === selectedKey;
      return React.createElement(DayCell, {
        key: i,
        day: cell.day,
        isOther: cell.isOther,
        isToday: isToday,
        isSelected: isSelected,
        tasks: dayTasks,
        onSelect: function () { onSelectDay(cell.dateKey); },
      });
    })
  );
}

/* ── DayCell ─────────────────────────────────────────────────────────── */

function DayCell({ day, isOther, isToday, isSelected, tasks, onSelect }) {
  var cls = "tasks-cal-day";
  if (isOther) cls += " tasks-cal-day--other";
  if (isToday) cls += " tasks-cal-day--today";
  if (isSelected) cls += " tasks-cal-day--selected";

  var dots = tasks.slice(0, 3).map(function (task, i) {
    var dotCls = "tasks-cal-task-dot";
    dotCls += task.status === "active" ? " tasks-cal-task-dot--active"
      : " tasks-cal-task-dot--paused";
    if (task.isRecurring) dotCls += " tasks-cal-task-dot--recurring";
    return React.createElement("span", { key: i, className: dotCls });
  });
  var overflow = tasks.length > 3
    ? React.createElement("span", { className: "tasks-cal-task-more" }, "+" + (tasks.length - 3))
    : null;

  return React.createElement("div", { className: cls, onClick: onSelect },
    React.createElement("div", { className: "tasks-cal-day-num" }, day),
    (tasks.length > 0) && React.createElement("div", { className: "tasks-cal-task-dots" },
      dots, overflow
    )
  );
}

/* ── DayPopover ──────────────────────────────────────────────────────── */

function DayPopover({ dateKey, tasks, onClose, onEdit, onToggle, onDelete }) {
  var { t } = window.useI18n();
  var lang = window.__i18nLang === "zh" ? "zh-CN" : "en-US";
  var d = new Date(dateKey + "T00:00:00");
  var heading = isNaN(d.getTime())
    ? dateKey
    : d.toLocaleDateString(lang, { weekday: "long", month: "long", day: "numeric" });

  var items = tasks.length > 0
    ? tasks.map(function (task) {
        var dotCls = "tasks-status-dot";
        dotCls += task.status === "active" ? " status-active" : " status-paused";
        var timeStr = formatTime(task.next_run);
        return React.createElement("div", { key: task.id, className: "tasks-cal-popover-item" },
          React.createElement("div", { className: "tasks-cal-popover-item-prompt" },
            React.createElement("span", { className: dotCls }),
            React.createElement("span", { className: "tasks-cal-popover-item-text" }, task.prompt),
            task.isRecurring && React.createElement("span", { className: "tasks-cal-recurring-badge" },
              t("tasks.calendar.repeats")
            )
          ),
          React.createElement("div", { className: "tasks-cal-popover-item-meta" },
            React.createElement("span", null, t("tasks.calendar.schedule"), ": ", scheduleLabel(task)),
            timeStr && React.createElement("span", null, "· " + timeStr),
            React.createElement("span", null, "· " + t("tasks.calendar.status"), ": ",
              task.status === "active" ? "Active" : "Paused"
            )
          ),
          React.createElement("div", { className: "tasks-cal-popover-item-actions" },
            React.createElement("button", {
              onClick: function () { onClose(); onEdit(task); }
            }, t("tasks.edit")),
            React.createElement("button", {
              onClick: function () { onClose(); onToggle(task); }
            }, task.status === "active" ? "⏸" : "▶"),
            React.createElement("button", {
              className: "tasks-action-delete",
              onClick: function () { onClose(); onDelete(task.id); }
            }, t("tasks.delete"))
          )
        );
      })
    : React.createElement("div", { className: "tasks-cal-popover-empty" },
        t("tasks.calendar.noTasks")
      );

  return React.createElement("div", {
    className: "tasks-cal-popover-overlay",
    onClick: function (e) { if (e.target === e.currentTarget) onClose(); }
  },
    React.createElement("div", { className: "tasks-cal-popover", onClick: function (e) { e.stopPropagation(); } },
      React.createElement("div", { className: "tasks-cal-popover-head" },
        React.createElement("span", null, heading),
        React.createElement("button", { onClick: onClose }, "×")
      ),
      items
    )
  );
}

window.TaskCalendarView = CalendarView;
