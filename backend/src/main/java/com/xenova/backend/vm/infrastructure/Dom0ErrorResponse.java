package com.xenova.backend.vm.infrastructure;

// dom0-manager error body (contract 8.1). platform_code is the lower platform's own code when the error came from it
// (e.g. VM_NOT_STOPPED); it is absent for errors dom0-manager raises itself.
record Dom0ErrorResponse(String code, String message, String detail, ResourceError resource, String platformCode) {

    record ResourceError(String name, Integer requested, Integer available) {
    }
}
