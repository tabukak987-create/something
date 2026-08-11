package com.poi.investment.analysis;

import java.math.BigDecimal;
import java.time.Instant;
import java.util.List;
import java.util.Map;

public final class AnalysisModels {
  private AnalysisModels() {}

  public record ParsedInstrument(
      String isin,
      String issuer,
      BigDecimal couponRate,
      BigDecimal marketPrice,
      String rating,
      BigDecimal quantity,
      BigDecimal faceValue,
      String sourceFile,
      int sourceRow) {}

  public record FileResult(
      String filename,
      String type,
      long sizeBytes,
      int extractedRows,
      String status,
      List<String> notes) {}

  public record InstrumentResult(
      String isin,
      String issuer,
      BigDecimal couponRate,
      BigDecimal marketPrice,
      String rating,
      BigDecimal quantity,
      BigDecimal faceValue,
      BigDecimal marketValue,
      int completeness,
      List<String> sources) {}

  public record Finding(String severity, String category, String title, String detail) {}

  public record Summary(
      int fileCount,
      int instrumentCount,
      BigDecimal totalMarketValue,
      BigDecimal weightedCoupon,
      int dataQualityScore,
      int investmentGradePercent,
      String decision) {}

  public record AnalysisResult(
      String id,
      String name,
      String status,
      String createdBy,
      Instant createdAt,
      Instant completedAt,
      Summary summary,
      List<FileResult> files,
      List<InstrumentResult> instruments,
      List<Finding> findings,
      Map<String, BigDecimal> issuerExposure,
      Map<String, Integer> ratingDistribution,
      List<String> recommendations) {}

  public record HistoryItem(
      String id,
      String name,
      String status,
      Instant createdAt,
      int fileCount,
      int instrumentCount,
      BigDecimal totalMarketValue,
      int dataQualityScore,
      String decision) {}
}
