package com.xenova.backend.display.mode;

import com.fasterxml.jackson.databind.JsonNode;

/** Vehicle display mode (HDMI monitor or USB phone) on the board, through dom0-manager (dom0 contract 4.14). */
public interface DisplayModePort {

    /** Current mode and the state of the display services. */
    JsonNode status();

    /** Runs the board's switch to {@code hdmi} or {@code mobile} and returns the state after it. May take minutes. */
    JsonNode switchTo(String mode);
}
