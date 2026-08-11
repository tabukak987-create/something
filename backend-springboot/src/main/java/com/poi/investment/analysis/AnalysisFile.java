package com.poi.investment.analysis;

import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Lob;
import jakarta.persistence.Table;

@Entity
@Table(name = "analysis_files")
public class AnalysisFile {
  @Id @GeneratedValue(strategy = GenerationType.IDENTITY) private Long id;
  private String batchId;
  private String filename;
  private String contentType;
  private long sizeBytes;
  private String checksum;
  private int extractedRows;
  @Lob @jakarta.persistence.Column(columnDefinition = "LONGBLOB") private byte[] content;

  protected AnalysisFile() {}

  public AnalysisFile(String batchId, String filename, String contentType, long sizeBytes,
      String checksum, int extractedRows, byte[] content) {
    this.batchId = batchId;
    this.filename = filename;
    this.contentType = contentType;
    this.sizeBytes = sizeBytes;
    this.checksum = checksum;
    this.extractedRows = extractedRows;
    this.content = content;
  }
}
