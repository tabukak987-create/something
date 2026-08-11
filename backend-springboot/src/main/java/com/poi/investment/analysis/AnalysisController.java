package com.poi.investment.analysis;

import java.nio.charset.StandardCharsets;
import java.security.Principal;
import java.util.List;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

@RestController
@RequestMapping("/api/v1/analyses")
public class AnalysisController {
  private final AnalysisService service;

  public AnalysisController(AnalysisService service) { this.service = service; }

  @PostMapping(consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
  public AnalysisModels.AnalysisResult run(
      @RequestParam(required = false) String name,
      @RequestParam("files") List<MultipartFile> files,
      Principal principal) {
    return service.run(name, files, principal.getName());
  }

  @GetMapping
  public List<AnalysisModels.HistoryItem> history(Principal principal) {
    return service.history(principal.getName());
  }

  @GetMapping("/{id}")
  public AnalysisModels.AnalysisResult get(@PathVariable String id, Principal principal) {
    return service.get(id, principal.getName());
  }

  @GetMapping("/template.csv")
  public ResponseEntity<byte[]> template() {
    String csv = "isin,issuer,couponRate,marketPrice,rating,quantity,faceValue\n"
        + "IN0000000001,Fictional Infrastructure Ltd,7.85,101.25,AA+,1000,1000\n"
        + "IN0000000002,Fictional Finance Ltd,8.40,98.75,A,750,1000\n";
    return ResponseEntity.ok()
        .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=poi-analysis-template.csv")
        .contentType(MediaType.parseMediaType("text/csv"))
        .body(csv.getBytes(StandardCharsets.UTF_8));
  }
}
