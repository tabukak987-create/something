package com.poi.investment.system;

import jakarta.servlet.http.HttpServletRequest;
import java.time.Instant;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ResponseStatusException;

@RestControllerAdvice
public class ApiExceptionHandler {
  @ExceptionHandler(ResponseStatusException.class)
  ResponseEntity<Map<String, Object>> status(ResponseStatusException exception, HttpServletRequest request) {
    return response(exception.getStatusCode().value(), exception.getReason(), request.getRequestURI());
  }

  @ExceptionHandler(MethodArgumentNotValidException.class)
  ResponseEntity<Map<String, Object>> validation(MethodArgumentNotValidException exception, HttpServletRequest request) {
    String message = exception.getBindingResult().getFieldErrors().stream()
        .findFirst().map(error -> error.getField() + ": " + error.getDefaultMessage()).orElse("Validation failed");
    return response(400, message, request.getRequestURI());
  }

  @ExceptionHandler(Exception.class)
  ResponseEntity<Map<String, Object>> unexpected(Exception exception, HttpServletRequest request) {
    return response(500, "The request could not be completed", request.getRequestURI());
  }

  private ResponseEntity<Map<String, Object>> response(int status, String message, String path) {
    return ResponseEntity.status(status).body(Map.of(
        "timestamp", Instant.now().toString(),
        "status", status,
        "error", HttpStatus.valueOf(status).getReasonPhrase(),
        "message", message == null ? "Request failed" : message,
        "path", path));
  }
}
