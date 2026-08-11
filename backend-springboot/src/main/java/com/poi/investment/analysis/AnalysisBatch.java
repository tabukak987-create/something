package com.poi.investment.analysis;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;
import java.math.BigDecimal;
import java.time.Instant;

@Entity
@Table(name = "analysis_batches")
public class AnalysisBatch {
  @Id private String id;
  @Column(nullable = false) private String name;
  @Column(nullable = false) private String createdBy;
  @Column(nullable = false) private String status;
  @Column(nullable = false) private Instant createdAt;
  private Instant completedAt;
  private int fileCount;
  private int instrumentCount;
  private BigDecimal totalMarketValue;
  private int dataQualityScore;
  private String decision;
  @Lob @Column(columnDefinition = "LONGTEXT", nullable = false) private String resultJson;

  protected AnalysisBatch() {}

  public AnalysisBatch(String id, String name, String createdBy, Instant createdAt) {
    this.id = id;
    this.name = name;
    this.createdBy = createdBy;
    this.createdAt = createdAt;
    this.status = "PROCESSING";
    this.resultJson = "{}";
  }

  public void complete(AnalysisModels.AnalysisResult result, String json) {
    this.status = "COMPLETED";
    this.completedAt = Instant.now();
    this.fileCount = result.summary().fileCount();
    this.instrumentCount = result.summary().instrumentCount();
    this.totalMarketValue = result.summary().totalMarketValue();
    this.dataQualityScore = result.summary().dataQualityScore();
    this.decision = result.summary().decision();
    this.resultJson = json;
  }

  public String getId() { return id; }
  public String getName() { return name; }
  public String getCreatedBy() { return createdBy; }
  public String getStatus() { return status; }
  public Instant getCreatedAt() { return createdAt; }
  public Instant getCompletedAt() { return completedAt; }
  public int getFileCount() { return fileCount; }
  public int getInstrumentCount() { return instrumentCount; }
  public BigDecimal getTotalMarketValue() { return totalMarketValue; }
  public int getDataQualityScore() { return dataQualityScore; }
  public String getDecision() { return decision; }
  public String getResultJson() { return resultJson; }
}
