package com.xenova.backend.template.domain;

// What Spring adds to a board template: the board catalog has no display name or minimum memory.
// Keyed by template_id only; versions come from the board.
public record TemplateMetadata(String templateId, String displayName, int minMemoryMib, GuestKind guestKind) {

    public TemplateMetadata {
        guestKind = guestKind == null ? GuestKind.LINUX : guestKind;
    }
}
