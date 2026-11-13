package com.xenova.backend.drive.api;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.xenova.backend.agent.application.AutoDelayCommand;
import com.xenova.backend.drive.application.ActiveDriveAutoDelayService;
import java.time.Instant;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

class DriveFaultControllerTest {

    private static final String ACTIVE_VM = "850e8400-e29b-41d4-a716-446655440001";
    private static final Instant NOW = Instant.parse("2026-11-13T00:00:00Z");
    private final ActiveDriveAutoDelayService service = mock(ActiveDriveAutoDelayService.class);
    private MockMvc mvc;

    @BeforeEach
    void setUp() {
        ObjectMapper mapper = new ObjectMapper().findAndRegisterModules()
                .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS)
                .setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
        mvc = MockMvcBuilders.standaloneSetup(new DriveFaultController(service))
                .setMessageConverters(new MappingJackson2HttpMessageConverter(mapper)).build();
    }

    @Test
    void sendsDelayWithoutAcceptingAClientSelectedVm() throws Exception {
        when(service.send(50, 30)).thenReturn(new AutoDelayCommand(
                "command-1", "operation-1", ACTIVE_VM, 50, 30, NOW, NOW.plusSeconds(30)));

        mvc.perform(post("/api/v1/drive/faults/auto-delay")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"delay_ms\":50,\"duration_s\":30}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.command_id").value("command-1"))
                .andExpect(jsonPath("$.vm_id").value(ACTIVE_VM))
                .andExpect(jsonPath("$.delay_ms").value(50))
                .andExpect(jsonPath("$.duration_s").value(30));

        verify(service).send(50, 30);
    }

    @Test
    void rejectsOutOfRangeDelayBeforeCallingTheService() throws Exception {
        mvc.perform(post("/api/v1/drive/faults/auto-delay")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"delay_ms\":101,\"duration_s\":30}"))
                .andExpect(status().isBadRequest());

        verifyNoInteractions(service);
    }
}
