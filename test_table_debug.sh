#!/bin/bash
# 表格重叠问题调试脚本

PDF_FILE="APEX-PF-EN.pdf"  # 替换为您的PDF文件路径
OUTPUT_LOG="table_debug_$(date +%Y%m%d_%H%M%S).log"

echo "=========================================="
echo "表格翻译调试"
echo "输入文件: $PDF_FILE"
echo "输出日志: $OUTPUT_LOG"
echo "=========================================="
echo ""

# 运行翻译，只翻译第2页，启用DEBUG日志
paper_agent "$PDF_FILE" -p 2 --log-level DEBUG 2>&1 | tee "$OUTPUT_LOG"

echo ""
echo "=========================================="
echo "调试完成！"
echo "请查看日志文件中的以下关键字："
echo "  - [PAGE]     : 页面处理信息"
echo "  - [TABLE]    : 表格处理信息"
echo "  - table_char : 表格字符统计"
echo "  - LAYOUT     : 段落布局信息"
echo "=========================================="
