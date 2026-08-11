package com.poi.investment.analysis;

import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.springframework.mock.web.MockMultipartFile;

class AnalysisParserServiceTest {
  private final AnalysisParserService parser = new AnalysisParserService(new ObjectMapper());

  @Test
  void parsesFlexibleCsvHeadersUsingDecimals() throws Exception {
    String csv = "ISIN,Issuer,Coupon Rate,Market Price,Credit Rating,Quantity,Face Value\n"
        + "IN0000000001,Fictional Infrastructure Ltd,7.85,101.25,AA+,1000,1000\n";
    var file = new MockMultipartFile("files", "portfolio.csv", "text/csv",
        csv.getBytes(StandardCharsets.UTF_8));

    var result = parser.parse(file);

    assertThat(result.type()).isEqualTo("CSV");
    assertThat(result.instruments()).hasSize(1);
    assertThat(result.instruments().getFirst().isin()).isEqualTo("IN0000000001");
    assertThat(result.instruments().getFirst().couponRate()).hasToString("7.85");
    assertThat(result.instruments().getFirst().quantity()).hasToString("1000");
  }
}
