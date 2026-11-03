"""Stable public task failures; never pass arbitrary exceptions or paths to users."""
import re

MESSAGES={
 'no_selected_fragments':'请先选择并放置至少一片裁片。',
 'source_warning_confirmation_required':'原文件存在待确认警告，请查看并确认后再导出。',
 'source_semantics_not_faithful':'原文件含暂时无法保真的内容，请保留草稿并反馈原文件。',
 'invalid_source_identity':'原件与工程版本不一致，请保留草稿并重新读取原附件。',
 'invalid_source_fragment':'裁片片段引用无效，请保留草稿并重新读取原附件。',
 'invalid_numeric_value':'排版坐标或参数无效，请保留草稿并检查当前排版。',
 'label_pose_not_faithful':'设备文字旋转或镜像暂无法保真，请保留草稿并调整该片姿态后再试。',
 'line_type_phase_not_faithful':'该设备线型的截取相位暂无法保真，请保留草稿并反馈原文件。',
 'label_control_not_supported':'原文件文字控制指令暂无法保真导出，请保留草稿并反馈原文件。',
 'recognition_failed':'原文件暂未能完成识别，请保留原件并重试。',
 'export_failed':'排版文件未能安全生成，请保留草稿后重试。',
 'result_storage_budget_exceeded':'排版结果超过服务临时存储预算，请保留原件和草稿并稍后重试。',
 'storage_unavailable':'排版服务临时存储空间不足，请保留草稿并稍后重试。',
 'wall_time_exceeded':'本次排版处理超时并已停止，请保留原件和草稿后重试。',
 'worker_crash_or_memory_budget':'本次排版处理异常或超过资源预算并已停止，请保留原件和草稿后重试。',
 'worker_restart_interrupted':'排版服务重启中断了本次处理，请保留草稿后重试。',
 'supervisor_error':'排版任务运行异常，请保留草稿后重试。',
 'worker_did_not_finalize':'排版任务未能安全完成，请保留草稿后重试。',
 'task_cancelled':'本次排版处理已取消，原件和草稿保留。',
 'configuration_required':'排版服务尚未完成配置，请保留草稿并稍后重试。',
 'idempotency_conflict':'本次请求编号已用于不同内容，请保留草稿后重新提交。',
}


def public_task_error(error,kind):
    code=getattr(error,'code',None)
    if code in MESSAGES:return code,MESSAGES[code]
    if isinstance(error,MemoryError):return 'worker_crash_or_memory_budget',MESSAGES['worker_crash_or_memory_budget']
    if isinstance(error,ValueError) and str(error)=='operator-configured result storage budget exceeded':return 'result_storage_budget_exceeded',MESSAGES['result_storage_budget_exceeded']
    if isinstance(error,ValueError) and re.fullmatch(r'PLT (?:命令数量过多，最多支持 \d+ 条|坐标点过多，最多支持 \d+ 个|路径过多，最多支持 \d+ 条|文本内容过多，最多支持 \d+ 个字符)',str(error)):
        return 'source_complexity_limit',str(error)+'；请保留原件并反馈该文件。'
    code='recognition_failed' if kind=='recognize' else 'export_failed'
    return code,MESSAGES[code]
