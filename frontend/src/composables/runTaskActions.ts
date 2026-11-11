// CloudSense（云听）· Copyright (c) 2026 keanying
// SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
// 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
// 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
// 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

/**
 * 运行记录上对「所属任务」的操作：立即开始、停止 / 取消排队、暂停 / 启用定时。
 * 运行记录列表和运行详情共用。操作的是**任务**，一个任务的几条记录上按钮一样。
 */
import { ElMessageBox } from 'element-plus'
import { taskApi, type TaskRun } from '../api'
import { toast } from './toast'

/** 任务现在在跑 / 已派出去 / 在排队：给「停止」，否则给「立即开始」 */
export function taskIsActive(run: Pick<TaskRun, 'task_status'>): boolean {
  return ['running', 'queued', 'waiting'].includes(run.task_status || '')
}

export function stopTip(run: Pick<TaskRun, 'task_status'>): string {
  return run.task_status === 'waiting' ? '取消排队（这一轮不再执行）' : '停止运行（已采到的数据保留）'
}

/** 周期任务才有「暂停定时 / 启用定时」 */
export function isPeriodic(run: Pick<TaskRun, 'task_schedule_type'>): boolean {
  return !!run.task_schedule_type && run.task_schedule_type !== 'once'
}

export async function startTask(run: TaskRun): Promise<boolean> {
  await taskApi.run(run.task_id)
  toast.success('已开始执行', {
    description: run.task_status === 'waiting' ? '跳过排队，现在就去拿号开跑' : (run.task_name || ''),
  })
  return true
}

export async function stopTask(run: TaskRun): Promise<boolean> {
  const waiting = run.task_status === 'waiting'
  try {
    await ElMessageBox.confirm(
      waiting ? '取消这个任务的排队？这一轮不再执行，定时任务到下一次时间照常跑。'
        : '停止这个任务？正在采的这一条做完就停，已采到的数据会保留。',
      waiting ? '取消排队' : '停止运行',
      { type: 'warning', confirmButtonText: waiting ? '取消排队' : '停止' },
    )
  } catch {
    return false
  }
  const r = await taskApi.cancel(run.task_id)
  if (r.cancelled) toast.success('已发送停止信号', { description: '任务会在几秒内停下' })
  else if (r.dequeued) toast.success('已取消排队', { description: '这一轮不再执行' })
  else toast.info('任务当前不在运行')
  return true
}

export async function toggleTaskSchedule(run: TaskRun): Promise<boolean> {
  const enable = !run.task_schedule_enabled
  await taskApi.toggleSchedule(run.task_id, enable)
  toast.success(enable ? '已启用定时' : '已暂停定时',
    { description: enable ? '到点会照常执行' : '不再自动执行，需要时可以手动立即开始' })
  return true
}
