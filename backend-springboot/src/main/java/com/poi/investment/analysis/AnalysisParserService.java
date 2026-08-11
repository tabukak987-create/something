package com.poi.investment.analysis;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.ByteArrayInputStream;
import java.io.InputStreamReader;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.commons.csv.CSVFormat;
import org.apache.commons.csv.CSVRecord;
import org.apache.pdfbox.Loader;
import org.apache.pdfbox.pdmodel.PDDocument;
import org.apache.pdfbox.text.PDFTextStripper;
import org.apache.poi.ss.usermodel.DataFormatter;
import org.apache.poi.ss.usermodel.Row;
import org.apache.poi.ss.usermodel.Sheet;
import org.apache.poi.ss.usermodel.Workbook;
import org.apache.poi.xssf.usermodel.XSSFWorkbook;
import org.springframework.stereotype.Service;
import org.springframework.web.multipart.MultipartFile;

@Service
public class AnalysisParserService {
  private static final Pattern ISIN = Pattern.compile("\\bIN[A-Z0-9]{10}\\b", Pattern.CASE_INSENSITIVE);
  private static final Pattern COUPON = Pattern.compile("(?i)(?:coupon|interest rate)\\s*[:\\-]?\\s*(\\d{1,2}(?:\\.\\d+)?)\\s*%");
  private static final Pattern RATING = Pattern.compile("\\b(AAA|AA[+-]?|A[+-]?|BBB[+-]?|BB[+-]?|B[+-]?|CCC|CC|C|D)\\b", Pattern.CASE_INSENSITIVE);
  private static final Pattern ISSUER = Pattern.compile("(?im)^\\s*(?:issuer|company|borrower)\\s*[:\\-]\\s*(.{3,120})$");
  private final ObjectMapper mapper;

  public AnalysisParserService(ObjectMapper mapper) { this.mapper = mapper; }

  public ParsedFile parse(MultipartFile file) throws Exception {
    String filename = file.getOriginalFilename() == null ? "upload" : file.getOriginalFilename();
    String lower = filename.toLowerCase(Locale.ROOT);
    byte[] bytes = file.getBytes();
    if (lower.endsWith(".csv")) return parseCsv(filename, bytes);
    if (lower.endsWith(".xlsx")) return parseXlsx(filename, bytes);
    if (lower.endsWith(".json")) return parseJson(filename, bytes);
    if (lower.endsWith(".pdf")) return parseText(filename, extractPdf(bytes), "PDF");
    if (lower.endsWith(".txt")) return parseText(filename, new String(bytes, StandardCharsets.UTF_8), "TEXT");
    throw new IllegalArgumentException("Unsupported file type for " + filename + ". Use CSV, XLSX, JSON, PDF, or TXT.");
  }

  private ParsedFile parseCsv(String filename, byte[] bytes) throws Exception {
    List<AnalysisModels.ParsedInstrument> rows = new ArrayList<>();
    try (var reader = new InputStreamReader(new ByteArrayInputStream(bytes), StandardCharsets.UTF_8);
         var parser = CSVFormat.DEFAULT.builder().setHeader().setSkipHeaderRecord(true).get().parse(reader)) {
      int row = 1;
      for (CSVRecord record : parser) {
        Map<String, String> values = new HashMap<>();
        record.toMap().forEach((key, value) -> values.put(normalizeKey(key), value));
        AnalysisModels.ParsedInstrument instrument = fromMap(values, filename, ++row);
        if (hasData(instrument)) rows.add(instrument);
      }
    }
    return new ParsedFile("CSV", rows, rows.isEmpty() ? List.of("No recognizable instrument rows found") : List.of());
  }

  private ParsedFile parseXlsx(String filename, byte[] bytes) throws Exception {
    List<AnalysisModels.ParsedInstrument> rows = new ArrayList<>();
    List<String> notes = new ArrayList<>();
    DataFormatter formatter = new DataFormatter();
    try (Workbook workbook = new XSSFWorkbook(new ByteArrayInputStream(bytes))) {
      for (int sheetIndex = 0; sheetIndex < workbook.getNumberOfSheets(); sheetIndex++) {
        Sheet sheet = workbook.getSheetAt(sheetIndex);
        Iterator<Row> iterator = sheet.rowIterator();
        if (!iterator.hasNext()) continue;
        Row header = iterator.next();
        List<String> headers = new ArrayList<>();
        for (int column = 0; column < header.getLastCellNum(); column++) {
          headers.add(normalizeKey(formatter.formatCellValue(header.getCell(column))));
        }
        while (iterator.hasNext()) {
          Row data = iterator.next();
          Map<String, String> values = new HashMap<>();
          for (int column = 0; column < headers.size(); column++) {
            values.put(headers.get(column), formatter.formatCellValue(data.getCell(column)));
          }
          AnalysisModels.ParsedInstrument instrument = fromMap(values, filename, data.getRowNum() + 1);
          if (hasData(instrument)) rows.add(instrument);
        }
      }
      if (workbook.getNumberOfSheets() > 1) notes.add("Combined " + workbook.getNumberOfSheets() + " workbook sheets");
    }
    if (rows.isEmpty()) notes.add("No recognizable instrument rows found");
    return new ParsedFile("EXCEL", rows, notes);
  }

  private ParsedFile parseJson(String filename, byte[] bytes) throws Exception {
    JsonNode root = mapper.readTree(bytes);
    JsonNode collection = root.isArray() ? root : firstArray(root);
    List<AnalysisModels.ParsedInstrument> rows = new ArrayList<>();
    if (collection != null) {
      int row = 0;
      for (JsonNode node : collection) {
        Map<String, String> values = new HashMap<>();
        node.fields().forEachRemaining(entry -> values.put(normalizeKey(entry.getKey()), entry.getValue().asText("")));
        AnalysisModels.ParsedInstrument instrument = fromMap(values, filename, ++row);
        if (hasData(instrument)) rows.add(instrument);
      }
    }
    return new ParsedFile("JSON", rows, rows.isEmpty() ? List.of("No recognizable instrument array found") : List.of());
  }

  private ParsedFile parseText(String filename, String text, String type) {
    List<AnalysisModels.ParsedInstrument> rows = new ArrayList<>();
    List<String> notes = new ArrayList<>();
    Matcher isinMatcher = ISIN.matcher(text);
    int index = 0;
    while (isinMatcher.find()) {
      String isin = isinMatcher.group().toUpperCase(Locale.ROOT);
      String window = text.substring(Math.max(0, isinMatcher.start() - 1000), Math.min(text.length(), isinMatcher.end() + 2500));
      rows.add(new AnalysisModels.ParsedInstrument(
          isin,
          match(ISSUER, window, stripExtension(filename)),
          decimal(match(COUPON, window, null)),
          null,
          match(RATING, window, null),
          BigDecimal.ONE,
          null,
          filename,
          ++index));
    }
    if (rows.isEmpty()) notes.add("Document stored, but no Indian ISIN could be extracted");
    else notes.add("Extracted document evidence for " + rows.size() + " instrument reference(s)");
    return new ParsedFile(type, rows, notes);
  }

  private AnalysisModels.ParsedInstrument fromMap(Map<String, String> values, String file, int row) {
    return new AnalysisModels.ParsedInstrument(
        value(values, "isin", "securityid", "instrumentid"),
        value(values, "issuer", "issuername", "company", "borrower"),
        decimal(value(values, "couponrate", "coupon", "interestrate")),
        decimal(value(values, "marketprice", "price", "cleanprice", "ltp")),
        upper(value(values, "rating", "creditrating")),
        decimal(value(values, "quantity", "units", "holding", "facequantity")),
        decimal(value(values, "facevalue", "parvalue", "nominalvalue")),
        file,
        row);
  }

  private JsonNode firstArray(JsonNode root) {
    if (root == null || !root.isObject()) return null;
    for (String key : List.of("instruments", "bonds", "holdings", "data", "records")) {
      if (root.has(key) && root.get(key).isArray()) return root.get(key);
    }
    return null;
  }

  private boolean hasData(AnalysisModels.ParsedInstrument item) {
    return item.isin() != null || item.issuer() != null || item.marketPrice() != null;
  }

  private String value(Map<String, String> values, String... keys) {
    for (String key : keys) {
      String value = clean(values.get(key));
      if (value != null) return value;
    }
    return null;
  }

  private String match(Pattern pattern, String text, String fallback) {
    Matcher matcher = pattern.matcher(text);
    return matcher.find() ? clean(matcher.group(1)) : fallback;
  }

  private BigDecimal decimal(String value) {
    if (value == null) return null;
    String normalized = value.replaceAll("[,%₹$£€\\s]", "");
    if (normalized.isBlank() || normalized.equals("-")) return null;
    try { return new BigDecimal(normalized); } catch (NumberFormatException ignored) { return null; }
  }

  private String normalizeKey(String value) {
    return value == null ? "" : value.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]", "");
  }

  private String clean(String value) {
    if (value == null || value.isBlank()) return null;
    return value.trim().replaceAll("\\s+", " ");
  }

  private String upper(String value) { return value == null ? null : value.toUpperCase(Locale.ROOT); }

  private String stripExtension(String filename) { return filename.replaceFirst("(?i)\\.[^.]+$", ""); }

  private String extractPdf(byte[] bytes) throws Exception {
    try (PDDocument document = Loader.loadPDF(bytes)) { return new PDFTextStripper().getText(document); }
  }

  public record ParsedFile(String type, List<AnalysisModels.ParsedInstrument> instruments, List<String> notes) {}
}
