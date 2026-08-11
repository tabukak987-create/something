package com.poi.investment.audit;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import java.time.Instant;
import java.util.UUID;

@Entity
@Table(name = "audit_events")
public class AuditEvent {
  @Id private String id;
  @Column(nullable = false) private Instant occurredAt;
  @Column(nullable = false) private String actor;
  @Column(nullable = false) private String action;
  @Column(nullable = false) private String entityType;
  @Column(nullable = false) private String entityId;
  @Column(nullable = false, length = 1000) private String detail;

  protected AuditEvent() {}

  public AuditEvent(String actor, String action, String entityType, String entityId, String detail) {
    this.id = UUID.randomUUID().toString();
    this.occurredAt = Instant.now();
    this.actor = actor;
    this.action = action;
    this.entityType = entityType;
    this.entityId = entityId;
    this.detail = detail;
  }
}
