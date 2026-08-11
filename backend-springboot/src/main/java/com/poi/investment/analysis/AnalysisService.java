package com.poi.investment.analysis;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.poi.investment.bond.Bond;
import com.poi.investment.bond.BondRepository;
import com.poi.investment.audit.AuditEvent;
import com.poi.investment.audit.AuditEventRepository;
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.server.ResponseStatusException;

@Service
public class AnalysisService {
  private static final Pattern VALID_ISIN = Pattern.compile("IN[A-Z0-9]{10}");
  private static final long MAX_FILE_BYTES = 20L * 1024 * 1024;
  private static final Set<String> ALLOWED_EXTENSIONS = Set.of("csv", "xlsx", "json", "pdf", "txt");

  private final AnalysisBatchRepository batches;
  private final AnalysisFileRepository storedFiles;
  private final BondRepository bonds;
  private final AnalysisParserService parser;
  private final ObjectMapper mapper;
  private final AuditEventRepository auditEvents;

  public AnalysisService(AnalysisBatchRepository batches, AnalysisFileRepository storedFiles,
      BondRepository bonds, AnalysisParserService parser, ObjectMapper mapper,
      AuditEventRepository auditEvents) {
    this.batches = batches;
    this.storedFiles = storedFiles;
    this.bonds = bonds;
    this.parser = parser;
    this.mapper = mapper;
    this.auditEvents = auditEvents;
  }

  @Transactional
  public AnalysisModels.AnalysisResult run(String requestedName, List<MultipartFile> files, String username) {
    validateFiles(files);
    String id = UUID.randomUUID().toString();
    String name = requestedName == null || requestedName.isBlank()
        ? "Investment review " + Instant.now().toString().substring(0, 10)
        : requestedName.trim().substring(0, Math.min(requestedName.trim().length(), 120));
    Instant createdAt = Instant.now();
    AnalysisBatch batch = batches.save(new AnalysisBatch(id, name, username, createdAt));

    List<AnalysisModels.FileResult> fileResults = new ArrayList<>();
    List<AnalysisModels.ParsedInstrument> allRows = new ArrayList<>();
    List<AnalysisModels.Finding> findings = new ArrayList<>();

    for (MultipartFile file : files) {
      String filename = safeFilename(file.getOriginalFilename());
      byte[] content;
      try { content = file.getBytes(); }
      catch (Exception exception) { throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Could not read " + filename); }

      int extractedRows = 0;
      String type = extension(filename).toUpperCase(Locale.ROOT);
      List<String> notes = new ArrayList<>();
      String status = "PROCESSED";
      try {
        AnalysisParserService.ParsedFile parsed = parser.parse(file);
        type = parsed.type();
        extractedRows = parsed.instruments().size();
        notes.addAll(parsed.notes());
        allRows.addAll(parsed.instruments());
        if (extractedRows == 0) status = "NEEDS_REVIEW";
      } catch (Exception exception) {
        status = "FAILED";
        notes.add(safeMessage(exception));
        findings.add(new AnalysisModels.Finding("HIGH", "FILE", "File could not be processed",
            filename + ": " + safeMessage(exception)));
      }

      storedFiles.save(new AnalysisFile(id, filename, file.getContentType(), content.length,
          sha256(content), extractedRows, content));
      fileResults.add(new AnalysisModels.FileResult(filename, type, content.length,
          extractedRows, status, notes));
    }

    MergeOutput merged = merge(allRows, findings);
    AnalysisModels.Summary summary = summarize(files.size(), merged.instruments(), findings);
    List<String> recommendations = recommendations(summary, findings);
    AnalysisModels.AnalysisResult result = new AnalysisModels.AnalysisResult(
        id, name, "COMPLETED", username, createdAt, Instant.now(), summary,
        fileResults, merged.instruments(), findings,
        issuerExposure(merged.instruments()), ratingDistribution(merged.instruments()), recommendations);

    persistRegister(merged.instruments());
    try {
      batch.complete(result, mapper.writeValueAsString(result));
      batches.save(batch);
      auditEvents.save(new AuditEvent(username, "ANALYSIS_COMPLETED", "ANALYSIS_BATCH", id,
          files.size() + " files combined into " + result.summary().instrumentCount()
              + " instruments; decision=" + result.summary().decision()));
    } catch (JsonProcessingException exception) {
      throw new IllegalStateException("Could not persist analysis result", exception);
    }
    return result;
  }

  @Transactional(readOnly = true)
  public List<AnalysisModels.HistoryItem> history(String username) {
    return batches.findTop20ByCreatedByOrderByCreatedAtDesc(username).stream()
        .map(batch -> new AnalysisModels.HistoryItem(batch.getId(), batch.getName(), batch.getStatus(),
            batch.getCreatedAt(), batch.getFileCount(), batch.getInstrumentCount(),
            zero(batch.getTotalMarketValue()), batch.getDataQualityScore(), batch.getDecision()))
        .toList();
  }

  @Transactional(readOnly = true)
  public AnalysisModels.AnalysisResult get(String id, String username) {
    AnalysisBatch batch = batches.findById(id)
        .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "Analysis not found"));
    if (!batch.getCreatedBy().equals(username)) {
      throw new ResponseStatusException(HttpStatus.NOT_FOUND, "Analysis not found");
    }
    try { return mapper.readValue(batch.getResultJson(), AnalysisModels.AnalysisResult.class); }
    catch (JsonProcessingException exception) { throw new IllegalStateException("Stored result is unreadable", exception); }
  }

  private MergeOutput merge(List<AnalysisModels.ParsedInstrument> rows, List<AnalysisModels.Finding> findings) {
    Map<String, MutableInstrument> merged = new LinkedHashMap<>();
    int unresolved = 0;
    for (AnalysisModels.ParsedInstrument row : rows) {
      String isin = normalizeIsin(row.isin());
      String key = isin != null ? isin : "UNRESOLVED-" + (++unresolved);
      if (isin == null) {
        findings.add(new AnalysisModels.Finding("HIGH", "DATA_QUALITY", "Missing or invalid ISIN",
            "A row in " + row.sourceFile() + " could not be matched to a valid Indian ISIN."));
      }
      MutableInstrument target = merged.computeIfAbsent(key, ignored -> new MutableInstrument(key));
      target.merge(row, findings);
    }

    List<AnalysisModels.InstrumentResult> results = merged.values().stream()
        .map(MutableInstrument::result)
        .sorted(Comparator.comparing(AnalysisModels.InstrumentResult::isin))
        .toList();

    for (AnalysisModels.InstrumentResult item : results) {
      List<String> missing = new ArrayList<>();
      if (item.issuer() == null) missing.add("issuer");
      if (item.couponRate() == null) missing.add("coupon");
      if (item.marketPrice() == null) missing.add("market price");
      if (item.rating() == null) missing.add("rating");
      if (!missing.isEmpty()) {
        findings.add(new AnalysisModels.Finding("MEDIUM", "DATA_QUALITY", "Incomplete instrument data",
            item.isin() + " is missing " + String.join(", ", missing) + "."));
      }
      if (item.rating() != null && ratingRank(item.rating()) < ratingRank("BBB-")) {
        findings.add(new AnalysisModels.Finding("HIGH", "CREDIT", "Below investment-grade rating",
            item.isin() + " is rated " + item.rating() + "; human credit review is required."));
      }
    }
    addConcentrationFindings(results, findings);
    return new MergeOutput(results);
  }

  private AnalysisModels.Summary summarize(int fileCount,
      List<AnalysisModels.InstrumentResult> instruments, List<AnalysisModels.Finding> findings) {
    BigDecimal total = instruments.stream().map(AnalysisModels.InstrumentResult::marketValue)
        .reduce(BigDecimal.ZERO, BigDecimal::add);
    BigDecimal weightedCouponNumerator = instruments.stream()
        .filter(item -> item.couponRate() != null)
        .map(item -> item.couponRate().multiply(item.marketValue()))
        .reduce(BigDecimal.ZERO, BigDecimal::add);
    BigDecimal weightedCoupon = total.signum() == 0 ? BigDecimal.ZERO
        : weightedCouponNumerator.divide(total, 4, RoundingMode.HALF_UP);
    int quality = instruments.isEmpty() ? 0 : (int) Math.round(instruments.stream()
        .mapToInt(AnalysisModels.InstrumentResult::completeness).average().orElse(0));
    long rated = instruments.stream().filter(item -> item.rating() != null).count();
    long investmentGrade = instruments.stream().filter(item -> item.rating() != null)
        .filter(item -> ratingRank(item.rating()) >= ratingRank("BBB-")).count();
    int gradePercent = rated == 0 ? 0 : (int) Math.round(investmentGrade * 100.0 / rated);
    boolean high = findings.stream().anyMatch(item -> item.severity().equals("HIGH"));
    String decision = instruments.isEmpty() || quality < 65
        ? "HOLD FOR ADDITIONAL INFORMATION"
        : high ? "RECOMMEND WITH CONDITIONS" : "READY FOR ANALYST REVIEW";
    return new AnalysisModels.Summary(fileCount, instruments.size(), money(total),
        weightedCoupon.setScale(2, RoundingMode.HALF_UP), quality, gradePercent, decision);
  }

  private Map<String, BigDecimal> issuerExposure(List<AnalysisModels.InstrumentResult> instruments) {
    Map<String, BigDecimal> exposure = new LinkedHashMap<>();
    for (AnalysisModels.InstrumentResult item : instruments) {
      String issuer = item.issuer() == null ? "Unknown issuer" : item.issuer();
      exposure.merge(issuer, item.marketValue(), BigDecimal::add);
    }
    return exposure;
  }

  private Map<String, Integer> ratingDistribution(List<AnalysisModels.InstrumentResult> instruments) {
    Map<String, Integer> distribution = new LinkedHashMap<>();
    for (AnalysisModels.InstrumentResult item : instruments) {
      distribution.merge(item.rating() == null ? "Not available" : item.rating(), 1, Integer::sum);
    }
    return distribution;
  }

  private void addConcentrationFindings(List<AnalysisModels.InstrumentResult> instruments,
      List<AnalysisModels.Finding> findings) {
    BigDecimal total = instruments.stream().map(AnalysisModels.InstrumentResult::marketValue)
        .reduce(BigDecimal.ZERO, BigDecimal::add);
    if (total.signum() == 0) return;
    issuerExposure(instruments).forEach((issuer, value) -> {
      BigDecimal percent = value.multiply(BigDecimal.valueOf(100)).divide(total, 2, RoundingMode.HALF_UP);
      if (percent.compareTo(BigDecimal.valueOf(25)) > 0) {
        findings.add(new AnalysisModels.Finding("HIGH", "CONCENTRATION", "High issuer concentration",
            issuer + " represents " + percent + "% of uploaded market value. No IPS limit was applied."));
      }
    });
  }

  private List<String> recommendations(AnalysisModels.Summary summary,
      List<AnalysisModels.Finding> findings) {
    List<String> items = new ArrayList<>();
    if (summary.instrumentCount() == 0) items.add("Upload a structured CSV, XLSX, or JSON file containing bond rows.");
    if (summary.dataQualityScore() < 80) items.add("Complete missing ISIN, issuer, coupon, price, and rating fields before approval.");
    if (findings.stream().anyMatch(item -> item.category().equals("CONFLICT"))) items.add("Resolve conflicting source values and document the selected official value.");
    if (findings.stream().anyMatch(item -> item.category().equals("CONCENTRATION"))) items.add("Review issuer exposure against the active Investment Policy Statement.");
    if (findings.stream().anyMatch(item -> item.category().equals("CREDIT"))) items.add("Perform a documented credit review for below investment-grade instruments.");
    items.add("All extracted and calculated outputs are drafts and require human investment review.");
    return items;
  }

  private void persistRegister(List<AnalysisModels.InstrumentResult> instruments) {
    for (AnalysisModels.InstrumentResult item : instruments) {
      if (!VALID_ISIN.matcher(item.isin()).matches()) continue;
      Bond bond = bonds.findById(item.isin()).orElseGet(() -> new Bond(item.isin(),
          item.issuer() == null ? "Unknown issuer" : item.issuer(), item.couponRate(), item.marketPrice(), item.rating()));
      bond.update(item.issuer() == null ? "Unknown issuer" : item.issuer(), item.couponRate(), item.marketPrice(), item.rating());
      bonds.save(bond);
    }
  }

  private void validateFiles(List<MultipartFile> files) {
    if (files == null || files.isEmpty()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Select at least one file");
    if (files.size() > 10) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "A batch may contain at most 10 files");
    for (MultipartFile file : files) {
      String name = safeFilename(file.getOriginalFilename());
      if (file.isEmpty()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, name + " is empty");
      if (file.getSize() > MAX_FILE_BYTES) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, name + " exceeds 20 MB");
      if (!ALLOWED_EXTENSIONS.contains(extension(name))) {
        throw new ResponseStatusException(HttpStatus.BAD_REQUEST, name + " has an unsupported file type");
      }
    }
  }

  private String normalizeIsin(String value) {
    if (value == null) return null;
    String normalized = value.replaceAll("[^A-Za-z0-9]", "").toUpperCase(Locale.ROOT);
    return VALID_ISIN.matcher(normalized).matches() ? normalized : null;
  }

  private static int ratingRank(String rating) {
    if (rating == null) return 0;
    return switch (rating.toUpperCase(Locale.ROOT).replaceAll("\\s", "")) {
      case "AAA" -> 18; case "AA+" -> 17; case "AA" -> 16; case "AA-" -> 15;
      case "A+" -> 14; case "A" -> 13; case "A-" -> 12;
      case "BBB+" -> 11; case "BBB" -> 10; case "BBB-" -> 9;
      case "BB+" -> 8; case "BB" -> 7; case "BB-" -> 6;
      case "B+" -> 5; case "B" -> 4; case "B-" -> 3;
      case "CCC" -> 2; case "CC", "C" -> 1; default -> 0;
    };
  }

  private String safeFilename(String name) {
    if (name == null || name.isBlank()) return "upload";
    return name.replace('\\', '/').substring(name.replace('\\', '/').lastIndexOf('/') + 1);
  }

  private String extension(String name) {
    int dot = name.lastIndexOf('.');
    return dot < 0 ? "" : name.substring(dot + 1).toLowerCase(Locale.ROOT);
  }

  private String sha256(byte[] bytes) {
    try { return java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
    catch (Exception exception) { throw new IllegalStateException("SHA-256 unavailable", exception); }
  }

  private String safeMessage(Exception exception) {
    String message = exception.getMessage();
    return message == null || message.isBlank() ? "Processing failed" : message.substring(0, Math.min(message.length(), 240));
  }

  private BigDecimal zero(BigDecimal value) { return value == null ? BigDecimal.ZERO : value; }
  private BigDecimal money(BigDecimal value) { return value.setScale(2, RoundingMode.HALF_UP); }

  private record MergeOutput(List<AnalysisModels.InstrumentResult> instruments) {}

  private static final class MutableInstrument {
    private final String isin;
    private String issuer;
    private BigDecimal coupon;
    private BigDecimal price;
    private String rating;
    private BigDecimal quantity = BigDecimal.ZERO;
    private BigDecimal faceValue;
    private final Set<String> sources = new LinkedHashSet<>();

    private MutableInstrument(String isin) { this.isin = isin; }

    private void merge(AnalysisModels.ParsedInstrument row, List<AnalysisModels.Finding> findings) {
      issuer = choose("issuer", issuer, row.issuer(), row, findings);
      coupon = chooseDecimal("coupon", coupon, row.couponRate(), row, findings);
      price = chooseDecimal("market price", price, row.marketPrice(), row, findings);
      rating = choose("rating", rating, row.rating(), row, findings);
      faceValue = chooseDecimal("face value", faceValue, row.faceValue(), row, findings);
      if (row.quantity() != null) quantity = quantity.add(row.quantity());
      sources.add(row.sourceFile());
    }

    private String choose(String field, String current, String incoming,
        AnalysisModels.ParsedInstrument row, List<AnalysisModels.Finding> findings) {
      if (incoming == null) return current;
      if (current != null && !current.equalsIgnoreCase(incoming)) conflict(field, current, incoming, row, findings);
      return current == null ? incoming : current;
    }

    private BigDecimal chooseDecimal(String field, BigDecimal current, BigDecimal incoming,
        AnalysisModels.ParsedInstrument row, List<AnalysisModels.Finding> findings) {
      if (incoming == null) return current;
      if (current != null && current.compareTo(incoming) != 0) conflict(field, current.toPlainString(), incoming.toPlainString(), row, findings);
      return current == null ? incoming : current;
    }

    private void conflict(String field, String current, String incoming,
        AnalysisModels.ParsedInstrument row, List<AnalysisModels.Finding> findings) {
      findings.add(new AnalysisModels.Finding("HIGH", "CONFLICT", "Conflicting " + field,
          isin + " has values " + current + " and " + incoming + "; " + row.sourceFile() + " row " + row.sourceRow() + " requires review."));
    }

    private AnalysisModels.InstrumentResult result() {
      BigDecimal effectiveQuantity = quantity.signum() == 0 ? BigDecimal.ONE : quantity;
      BigDecimal marketValue = price == null ? BigDecimal.ZERO : price.multiply(effectiveQuantity);
      int present = 1 + (issuer == null ? 0 : 1) + (coupon == null ? 0 : 1) + (price == null ? 0 : 1) + (rating == null ? 0 : 1);
      return new AnalysisModels.InstrumentResult(isin, issuer, coupon, price, rating,
          effectiveQuantity, faceValue, marketValue.setScale(2, RoundingMode.HALF_UP),
          present * 20, List.copyOf(sources));
    }
  }
}
