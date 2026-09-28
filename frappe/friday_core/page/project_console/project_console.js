// Copyright (c) 2026, Friday Labs and contributors
// For license information, please see license.txt
//
// The Project Console (design 65c) — Friday's live command center.
//
// One Desk page that answers the question the Legion test left unanswered:
// "what is actually happening in my projects right now?" Three zones:
//
//   1. Health strip   — the Pipeline Health verdict (is the loop alive?).
//   2. Project lane    — a card per project: status, %-complete, task counts.
//   3. Activity feed   — a live, push-driven stream of agent task transitions.
//
// Data flow (Q6): the feed is realtime-PUSH first — we subscribe to the
// site-wide `frappe:project_activity` event that `console_stream.publish_activity`
// emits on every Task transition, so rows appear the instant work happens with
// no polling. A 30s `console_snapshot` poll is the correctness backstop: it
// re-seeds all three zones from committed DB state, healing any dropped frame
// (state is truth, push is the optimization — the Design 61 principle, in UI).
//
// Compare with Hermes: its dashboard polls a flat session list every 5s. This
// pushes a project-scoped feed of governed transitions, each naming the agent
// (65a identity) and project. Push, not poll; a project plane, not a session list.

frappe.pages["project-console"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Project Console"),
		single_column: true,
	});

	const console_ui = new ProjectConsole(page);
	wrapper.project_console = console_ui;

	// A header button to jump to the Projects workspace / Kanban (65b).
	page.set_secondary_action(__("Task Pipeline"), () => {
		frappe.set_route("List", "Task", "Kanban", "Task Pipeline");
	});
};

// Re-seed whenever the user returns to an already-loaded page instance.
frappe.pages["project-console"].on_page_show = function (wrapper) {
	if (wrapper.project_console) {
		wrapper.project_console.refresh();
	}
};

class ProjectConsole {
	constructor(page) {
		this.page = page;
		this.project = null; // null = all projects
		this.feed = []; // most-recent-first activity rows
		this.MAX_FEED = 60;
		this.POLL_MS = 30000;

		// Build ONE debounced re-seed and reuse it. Creating a fresh debounced
		// fn per call (debounce(...)()) would never actually debounce.
		this._debounced_refresh = frappe.utils.debounce(() => this.refresh(), 1500);

		this._build_layout();
		this._subscribe_realtime();
		this._start_poll();
		this.refresh();
	}

	// ---- layout --------------------------------------------------------

	_build_layout() {
		this.$body = $(`
			<div class="friday-console">
				<div class="fc-health" data-zone="health"></div>
				<div class="fc-main">
					<div class="fc-projects-wrap">
						<div class="fc-section-title">${__("Projects")}
							<span class="fc-scope" data-zone="scope"></span>
						</div>
						<div class="fc-projects" data-zone="projects"></div>
						<div class="fc-section-title">${__("In Flight")}</div>
						<div class="fc-active" data-zone="active"></div>
					</div>
					<div class="fc-feed-wrap">
						<div class="fc-section-title">${__("Live Activity")}
							<span class="fc-live-dot" title="${__("Live")}"></span>
						</div>
						<div class="fc-feed" data-zone="feed"></div>
					</div>
				</div>
			</div>
		`).appendTo(this.page.body);

		this.$health = this.$body.find('[data-zone="health"]');
		this.$projects = this.$body.find('[data-zone="projects"]');
		this.$active = this.$body.find('[data-zone="active"]');
		this.$feed = this.$body.find('[data-zone="feed"]');
		this.$scope = this.$body.find('[data-zone="scope"]');

		// Clicking the scope chip clears the per-project filter.
		this.$scope.on("click", () => {
			if (this.project) {
				this.project = null;
				this.refresh();
			}
		});
	}

	// ---- data ----------------------------------------------------------

	refresh() {
		frappe.call({
			method: "frappe.friday_core.console.console_snapshot.console_snapshot",
			args: { project: this.project },
			callback: (r) => {
				if (!r || !r.message) return;
				this._render_snapshot(r.message);
			},
		});
	}

	_render_snapshot(snap) {
		if (snap.error) {
			this.$health.html(
				`<div class="fc-pill fc-down">● ${__("Console error")}: ${frappe.utils.escape_html(
					snap.error
				)}</div>`
			);
		} else {
			this._render_health(snap.health || {});
		}
		this._render_projects(snap.projects || []);
		this._render_active(snap.active_tasks || []);

		// Seed the feed from recent terminal activity ONLY if it's still empty
		// (live pushes take precedence once they start flowing).
		if (!this.feed.length) {
			this.feed = (snap.recent_activity || []).map((t) => ({
				task: t.name,
				title: t.title,
				state: t.workflow_state,
				profile: t.assigned_to_profile,
				project: t.project,
				is_terminal: true,
				ts: t.modified,
			}));
			this._render_feed();
		}
		this._render_scope();
	}

	// ---- zone: health --------------------------------------------------

	_render_health(health) {
		const verdict = health.verdict || "unknown";
		const cls =
			{ ok: "fc-ok", degraded: "fc-degraded", down: "fc-down" }[verdict] || "fc-unknown";
		const states = health.tasks_by_state || {};
		const friday_up = ((health.workers || {}).friday || {}).present;

		const count = (label, key, badge) =>
			`<span class="fc-count ${badge || ""}">${frappe.utils.escape_html(label)}
				<b>${states[key] || 0}</b></span>`;

		this.$health.html(`
			<div class="fc-pill ${cls}">● ${__(verdict).toUpperCase()}</div>
			${count(__("Pending"), "Pending")}
			${count(__("Executing"), "Executing", "fc-blue")}
			${count(__("Blocked"), "Blocked", "fc-red")}
			${count(__("Completed"), "Completed", "fc-green")}
			<span class="fc-worker ${friday_up ? "fc-green" : "fc-red"}">
				${__("friday worker")}: ${friday_up ? __("up") : __("absent")}</span>
		`);
	}

	// ---- zone: projects ------------------------------------------------

	_render_projects(projects) {
		if (!projects.length) {
			this.$projects.html(`<div class="fc-empty">${__("No projects yet.")}</div>`);
			return;
		}
		this.$projects.empty();
		projects.forEach((p) => {
			const pct = Math.round(p.percent_complete || 0);
			const status = p.status || "Open";
			const is_work_item = p.kind === "work_item";
			// A small chip on work-item tiles (named after their DocType) so users
			// see the metadata-engine work alongside the legacy Project rollups.
			const kind_chip = is_work_item
				? `<span class="fc-kind fc-kind-brief">${frappe.utils.escape_html(
						__(p.doctype)
				  )}</span>`
				: "";
			const card = $(`
				<div class="fc-card" data-project="${frappe.utils.escape_html(p.name)}">
					<div class="fc-card-head">
						<span class="fc-card-title">${frappe.utils.escape_html(p.project_name || p.name)}</span>
						${kind_chip}
						<span class="fc-status fc-status-${frappe.scrub(status)}">${__(status)}</span>
					</div>
					<div class="fc-progress"><div class="fc-progress-bar" style="width:${pct}%"></div></div>
					<div class="fc-card-foot">
						<span>${pct}% ${__("complete")}</span>
						<span>${p.completed_tasks || 0}/${p.total_tasks || 0} ${__("tasks")}</span>
					</div>
				</div>
			`);
			card.on("click", () => {
				if (is_work_item) {
					// Work-item tiles open the item's own form directly — the
					// snapshot's project-scope filter doesn't apply to engine
					// tasks (they're keyed by work_item, not Project).
					frappe.set_route("Form", p.doctype, p.name);
					return;
				}
				// Legacy Project tile: scope every zone to that project.
				this.project = p.name;
				this.feed = [];
				this.refresh();
			});
			this.$projects.append(card);
		});
	}

	// ---- zone: in-flight tasks -----------------------------------------

	_render_active(tasks) {
		if (!tasks.length) {
			this.$active.html(`<div class="fc-empty">${__("Nothing running right now.")}</div>`);
			return;
		}
		this.$active.html(
			tasks
				.map(
					(t) => `
			<div class="fc-active-row">
				<span class="fc-dot fc-blue-dot"></span>
				<span class="fc-active-agent">${frappe.utils.escape_html(
					t.assigned_to_profile || __("unassigned")
				)}</span>
				<span class="fc-active-title">${frappe.utils.escape_html(t.title || t.name)}</span>
				<span class="fc-active-state">${__(t.workflow_state)}</span>
			</div>`
				)
				.join("")
		);
	}

	// ---- zone: live feed -----------------------------------------------

	_render_scope() {
		this.$scope.text(this.project ? `— ${this.project} (×)` : "");
	}

	_render_feed() {
		if (!this.feed.length) {
			this.$feed.html(`<div class="fc-empty">${__("Waiting for activity…")}</div>`);
			return;
		}
		this.$feed.html(
			this.feed
				.map((e) => {
					const icon = e.is_terminal ? this._state_icon(e.state) : "▸";
					return `
				<div class="fc-feed-row ${e.is_terminal ? "fc-terminal" : ""}">
					<span class="fc-feed-icon">${icon}</span>
					<span class="fc-feed-agent">${frappe.utils.escape_html(e.profile || __("system"))}</span>
					<span class="fc-feed-text">${frappe.utils.escape_html(e.title || e.task)}
						<i>${__(e.state)}</i></span>
					<span class="fc-feed-time">${comment_when(e.ts)}</span>
				</div>`;
				})
				.join("")
		);
	}

	_state_icon(state) {
		return { Completed: "✅", Review: "📝", Blocked: "⛔", Cancelled: "🚫" }[state] || "•";
	}

	// ---- realtime ------------------------------------------------------

	_subscribe_realtime() {
		// Site-room event — every System User Desk client receives it without
		// any join (see console_stream + frappe realtime get_site_room).
		frappe.realtime.on("frappe:project_activity", (data) => {
			if (!data) return;
			// Respect the per-project filter if one is active.
			if (this.project && data.project !== this.project) return;
			this.feed.unshift(data);
			if (this.feed.length > this.MAX_FEED) this.feed.length = this.MAX_FEED;
			this._render_feed();
			// A terminal event likely changed counts/progress — cheap re-seed.
			if (data.is_terminal) {
				this._debounced_refresh();
			}
		});
	}

	_start_poll() {
		// 30s correctness backstop. Cleared when the page wrapper is destroyed.
		this._poll = setInterval(() => this.refresh(), this.POLL_MS);
		$(this.page.wrapper).on("remove", () => clearInterval(this._poll));
	}
}
