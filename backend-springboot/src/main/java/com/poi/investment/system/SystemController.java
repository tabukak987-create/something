package com.poi.investment.system;
import java.util.Map; import org.springframework.web.bind.annotation.*;
@RestController @RequestMapping("/api/v1") public class SystemController {@GetMapping("/health")Map<String,String> health(){return Map.of("status","UP","service","poi-investment-api");}}
