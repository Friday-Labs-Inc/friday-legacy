# Copyright (c) 2026, Friday Labs and contributors
# License: MIT. See license.txt

"""
Conversation surface — Friday's project conversation rooms (Design 73).

Style A (see [[interface-style-a-conversational]]): Friday is a hosted,
enterprise, multi-agent platform whose primary human interface is chat — not
a CLI (that's a personal-desktop tool's model) and not a dashboard-only Desk.
Each Project gets a dedicated Raven channel: the room where the agent team,
the human director, the deliverables, and the gate decisions live together.

Thin-skin principle: the Project row is the source of truth; the channel is a
*window* onto it. A Raven outage costs you the window, never the data.
"""

from frappe.friday_core.conversation.project_channel import (
	archive_project_channel,
	provision_project_channel,
)

__all__ = ["archive_project_channel", "provision_project_channel"]
