package com.poi.investment.analysis;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface AnalysisBatchRepository extends JpaRepository<AnalysisBatch, String> {
  List<AnalysisBatch> findTop20ByCreatedByOrderByCreatedAtDesc(String createdBy);
}
