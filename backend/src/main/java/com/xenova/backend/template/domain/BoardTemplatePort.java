package com.xenova.backend.template.domain;

import java.util.List;

// Reads the templates the board has published. Kept apart from VmControlPort so integration tests that mock
// VM control do not also lose the template list.
public interface BoardTemplatePort {

    List<BoardTemplate> listBoardTemplates();
}
