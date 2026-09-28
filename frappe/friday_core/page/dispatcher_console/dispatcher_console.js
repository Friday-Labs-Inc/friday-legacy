// Copyright (c) 2026, Friday Labs and contributors
// For license information, please see license.txt
//
// Dispatcher Console (Design 72) — framework-mechanics observability.
//
// Two tabs:
//   1. Pulse           — 2x3 grid of cells (scheduler, reconciler, leases,
//                        dispatchable, queues, workers). 2s polling.
//   2. Lifecycle Trace — per-task event timeline; auto-tail for live tasks.
//
// Design 72, Q5: polling, not socketio. The Raven `VITE_SOCKET_PORT=9000` vs
// 9005 incident proved an observability tool can't depend on the same fragile
// realtime layer it exists to observe. 2s polling is human-perceptible-live
// without the failure mode.

frappe.pages["dispatcher-console"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Dispatcher Console"),
		single_column: true,
	});

	const console_ui = new DispatcherConsole(page);
	wrapper.dispatcher_console = console_ui;

	page.set_secondary_action(__("Project Console"), () => {
		frappe.set_route("project-console");
	});
};

frappe.pages["dispatcher-console"].on_page_show = function (wrapper) {
	if (wrapper.dispatcher_console) {
		wrapper.dispatcher_console.activate();
	}
};

class DispatcherConsole {
	constructor(page) {
		this.page = page;
		this.POLL_MS = 2000;
		this.activeTab = "pulse";
		this.selectedTask = null;
		this.traceCursor = null;
		this.tailOn = true;
		this._pollTimer = null;

		this._buildLayout();
		this._wire();
		this._populateTaskPicker();
		this.activate();
	}

	// ---- layout --------------------------------------------------------

	_buildLayout() {
		this.$body = $(`
			<div class="fd-console">
				<div class="fd-tabs">
					<button class="fd-tab active" data-tab="pulse">${__("Pulse")}</button>
					<button class="fd-tab" data-tab="trace">${__("Lifecycle Trace")}</button>
				</div>

				<div class="fd-tab-body active" data-body="pulse">
					<div class="fd-pulse-grid">
						${this._cellHTML("scheduler", __("Scheduler"))}
						${this._cellHTML("reconciler", __("Reconciler"))}
						${this._cellHTML("active_leases", __("Active Leases"))}
						${this._cellHTML("dispatchable", __("Dispatchable Queue"))}
						${this._cellHTML("queues", __("RQ Queues"))}
						${this._cellHTML("workers", __("Workers"))}
					</div>
				</div>

				<div class="fd-tab-body" data-body="trace">
					<div class="fd-trace-bar">
						<select data-zone="task-picker"></select>
						<label class="fd-tail-toggle">
							<input type="checkbox" data-zone="tail-toggle" checked />
							${__("Auto-tail live tasks")}
						</label>
					</div>
					<div class="fd-trace-summary" data-zone="trace-summary" style="display: none"></div>
					<div class="fd-trace-list" data-zone="trace-list">
						<div class="fd-trace-empty">${__("Pick a task to inspect.")}</div>
					</div>
				</div>
			</div>
		`).appendTo(this.page.body);
	}

	_cellHTML(key, title) {
		return `
			<div class="fd-cell" data-cell="${key}">
				<div class="fd-cell-title">
					<span class="fd-status-dot idle"></span>
					${title}
				</div>
				<div class="fd-cell-headline">…</div>
				<div class="fd-cell-detail"></div>
			</div>
		`;
	}

	_wire() {
		this.$body.on("click", ".fd-tab", (ev) => {
			const tab = $(ev.currentTarget).data("tab");
			this._switchTab(tab);
		});
		this.$body.on("change", '[data-zone="task-picker"]', (ev) => {
			this.selectedTask = ev.target.value || null;
			this.traceCursor = null;
			this._fetchTrace(true);
		});
		this.$body.on("change", '[data-zone="tail-toggle"]', (ev) => {
			this.tailOn = ev.target.checked;
		});
	}

	_switchTab(tab) {
		this.activeTab = tab;
		this.$body.find(".fd-tab").removeClass("active");
		this.$body.find(`.fd-tab[data-tab="${tab}"]`).addClass("active");
		this.$body.find(".fd-tab-body").removeClass("active");
		this.$body.find(`[data-body="${tab}"]`).addClass("active");
		this._pollOnce();
	}

	// ---- polling -------------------------------------------------------

	activate() {
		this._pollOnce();
		this._startPolling();
	}

	_startPolling() {
		if (this._pollTimer) clearInterval(this._pollTimer);
		this._pollTimer = setInterval(() => this._pollOnce(), this.POLL_MS);
	}

	_pollOnce() {
		if (this.activeTab === "pulse") {
			this._fetchPulse();
		} else {
			if (this.selectedTask) this._fetchTrace(false);
		}
	}

	// ---- Pulse ---------------------------------------------------------

	_fetchPulse() {
		frappe
			.call({
				method: "frappe.friday_core.console.dispatcher_console_api.pulse",
				type: "GET",
			})
			.then((r) => {
				if (r && r.message) this._renderPulse(r.message);
			})
			.catch(() => {});
	}

	_renderPulse(p) {
		this._renderCell("scheduler", {
			headline: p.scheduler.headline || "—",
			detail: p.scheduler.last
				? `<b>${this._fmtAge(p.scheduler.age_seconds)}</b> ago · ${this._escape(
						p.scheduler.last
				  )}`
				: "",
			status: p.scheduler.status,
		});
		this._renderCell("reconciler", {
			headline: p.reconciler.headline || "—",
			detail: p.reconciler.last
				? `<b>${this._fmtAge(p.reconciler.age_seconds)}</b> ago · ${
						p.reconciler.total_actions || 0
				  } actions${
						p.reconciler.errors && p.reconciler.errors.length
							? ` · errors: ${this._escape(p.reconciler.errors.join(", "))}`
							: ""
				  }`
				: "",
			status: p.reconciler.status,
		});
		this._renderCell("active_leases", {
			headline: `${p.active_leases.count ?? "—"} executing`,
			detail:
				p.active_leases.stalest_task != null
					? `stalest <b>${this._escape(
							p.active_leases.stalest_task
					  )}</b> · ${this._fmtAge(
							p.active_leases.stalest_age_seconds
					  )} since heartbeat`
					: __("No tasks currently executing."),
			status: p.active_leases.status,
		});
		this._renderCell("dispatchable", {
			headline: `${p.dispatchable.count ?? "—"} unclaimed`,
			detail:
				p.dispatchable.oldest_task != null
					? `oldest <b>${this._escape(
							p.dispatchable.oldest_task
					  )}</b> · waited ${this._fmtAge(p.dispatchable.oldest_age_seconds)}`
					: __("Queue empty — dispatcher idle."),
			status: p.dispatchable.status,
		});
		this._renderCell("queues", {
			headline: this._fmtQueues(p.queues),
			detail: __("RQ pending jobs per queue."),
			status: p.queues.status,
		});
		this._renderCell("workers", {
			headline:
				(p.workers.profiles || []).length > 0
					? `${(p.workers.profiles || []).length} active`
					: __("none"),
			detail: (p.workers.profiles || [])
				.map((pr) => this._escape(pr.name))
				.slice(0, 4)
				.join(", "),
			status: p.workers.status,
		});
	}

	_renderCell(key, { headline, detail, status }) {
		const $cell = this.$body.find(`[data-cell="${key}"]`);
		$cell
			.find(".fd-status-dot")
			.removeClass("green amber red idle")
			.addClass(status || "idle");
		$cell.find(".fd-cell-headline").text(headline);
		$cell.find(".fd-cell-detail").html(detail || "");
	}

	_fmtQueues(q) {
		if (!q || q.status === "red") return __("error");
		const parts = [];
		if (q.default) parts.push(`default: <b>${q.default.depth ?? "?"}</b>`);
		if (q.friday) parts.push(`friday: <b>${q.friday.depth ?? "?"}</b>`);
		return parts.join(" · ") || "—";
	}

	// ---- Lifecycle Trace ----------------------------------------------

	_populateTaskPicker() {
		frappe
			.call({
				method: "frappe.friday_core.console.dispatcher_console_api.recent_tasks",
				args: { limit: 30 },
				type: "GET",
			})
			.then((r) => {
				const $sel = this.$body.find('[data-zone="task-picker"]');
				$sel.empty().append(`<option value="">${__("Pick a task…")}</option>`);
				(r.message || []).forEach((t) => {
					const label = `${t.name} — ${t.title || ""} (${t.workflow_state})`;
					$sel.append(
						`<option value="${this._escape(t.name)}">${this._escape(label)}</option>`
					);
				});
			})
			.catch(() => {});
	}

	_fetchTrace(reset) {
		if (!this.selectedTask) return;
		const args = { task: this.selectedTask };
		if (!reset && this.traceCursor) args.since_cursor = this.traceCursor;
		frappe
			.call({
				method: "frappe.friday_core.console.dispatcher_console_api.lifecycle_trace",
				args,
				type: "GET",
			})
			.then((r) => {
				if (!r || !r.message) return;
				this._renderTrace(r.message, reset);
				this.traceCursor = r.message.next_cursor || this.traceCursor;
			})
			.catch(() => {});
	}

	_renderTrace(data, reset) {
		const $list = this.$body.find('[data-zone="trace-list"]');
		const $summary = this.$body.find('[data-zone="trace-summary"]');

		// Summary panel
		if (data.summary || (data.task_state && Object.keys(data.task_state).length)) {
			const ts = data.task_state || {};
			const s = data.summary || {};
			$summary.show().html(`
				<div><div class="label">${__("State")}</div>${this._escape(
				ts.workflow_state || s.final_state || "—"
			)}</div>
				<div><div class="label">${__("Profile")}</div>${this._escape(ts.assigned_to_profile || "—")}</div>
				<div><div class="label">${__("Retries")}</div>${ts.retry_count ?? 0}</div>
				<div><div class="label">${__("Blocked Reason")}</div>${this._escape(
				ts.blocked_reason || s.blocked_reason || "—"
			)}</div>
				<div><div class="label">${__("Total Cost (USD)")}</div>${
				s.total_llm_cost_usd ? `$${Number(s.total_llm_cost_usd).toFixed(4)}` : "—"
			}</div>
				<div><div class="label">${__("Duration (ms)")}</div>${s.total_duration_ms ?? "—"}</div>
			`);
		} else {
			$summary.hide();
		}

		// Decide tail behavior: if reset, replace; else append.
		const liveState = data.task_state && data.task_state.is_live;
		if (reset) $list.empty();

		if (!data.events.length && reset) {
			$list.html(`<div class="fd-trace-empty">${__("No events for this task yet.")}</div>`);
			return;
		}

		const frag = document.createDocumentFragment();
		data.events.forEach((e) => {
			const div = document.createElement("div");
			const safeType = (e.event_type || "").replace(/\./g, "-");
			div.className = `fd-trace-row ${safeType}`;
			div.innerHTML = `
				<div class="ts">${this._fmtTime(e.creation)}</div>
				<div class="et">${this._escape(e.event_type)}<span class="src">${this._escape(
				e.trigger_source || ""
			)}</span></div>
				<div class="summary">${this._escape(e.summary || "")}</div>
			`;
			frag.appendChild(div);
		});

		// On a reset, clear the empty placeholder.
		if (reset && $list.find(".fd-trace-empty").length) $list.empty();
		$list[0].appendChild(frag);

		// Auto-scroll only when tailing a live task — operator inspecting
		// a frozen trace doesn't want their scroll position hijacked.
		if (this.tailOn && liveState) {
			$list[0].scrollTop = $list[0].scrollHeight;
		}
	}

	// ---- utils ---------------------------------------------------------

	_fmtAge(seconds) {
		if (seconds == null) return "—";
		if (seconds < 60) return `${seconds}s`;
		if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
		return `${Math.round(seconds / 3600)}h`;
	}

	_fmtTime(t) {
		if (!t) return "";
		// Frappe returns "YYYY-MM-DD HH:MM:SS.ffffff" — show HH:MM:SS only.
		const m = String(t).match(/(\d{2}:\d{2}:\d{2})/);
		return m ? m[1] : String(t);
	}

	_escape(s) {
		if (s == null) return "";
		return String(s)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;");
	}
}
