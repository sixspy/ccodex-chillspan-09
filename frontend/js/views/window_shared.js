// 窗协调面板：线路页、批次页、仓间页、封批页、对账页共用同一份 winCoord 数据。
// 单源原则：任何一页都不得自算窗，只渲染 /api/window-coordination 的返回。
export const windowStripTemplate = `
<div class="detail-box window-strip">
  <h3>线路 × 批次 窗协调（五页同源）</h3>
  <p class="hint">
    冲突落库只择一：甲=拒写零改，乙=影子草案待改线原子晋升。
    有效口径以本表 <strong>effective_source</strong> 为准，五页同读一张表。
  </p>
  <div class="form-row">
    <div><label>线路编码</label><input v-model="batchWindowForm.route_code"/></div>
    <div><label>批次</label>
      <select v-model.number="batchWindowForm.batch_id">
        <option v-for="b in batches" :key="b.id" :value="b.id">{{ b.batch_code }}</option>
      </select>
    </div>
    <div><label>批次窗起</label><input v-model="batchWindowForm.window_start"/></div>
    <div><label>批次窗讫</label><input v-model="batchWindowForm.window_end"/></div>
    <div><label>路径</label>
      <select v-model="batchWindowForm.decision">
        <option value="甲">甲 · 冲突拒写</option>
        <option value="乙">乙 · 影子草案</option>
      </select>
    </div>
  </div>
  <div class="actions">
    <button @click="submitBatchWindow">写批次窗</button>
    <button class="secondary" @click="loadWindowCoord">刷新窗协调</button>
  </div>
  <table class="data-table compact">
    <thead>
      <tr>
        <th>批次</th><th>线路</th><th>正式窗</th><th>影子</th>
        <th>有效源</th><th>有效</th><th>占用</th><th>冲突</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="w in winCoord.items" :key="w.batch_code">
        <td>{{ w.batch_code }}</td>
        <td>{{ w.route_code || "—" }}</td>
        <td>{{ w.official_window ? (w.official_window.start + " ~ " + w.official_window.end) : "—" }}</td>
        <td>{{ w.shadow ? (w.shadow.status + " " + w.shadow.window.start + "~" + w.shadow.window.end) : "—" }}</td>
        <td>{{ w.effective_source }}</td>
        <td><span class="badge" :class="w.effective_valid ? 'pass' : 'fail'">{{ w.effective_valid ? "有效" : "无效" }}</span></td>
        <td>{{ w.occupancy }}</td>
        <td><span v-if="w.conflict" class="bad">冲突</span><span v-else class="ok">无</span></td>
      </tr>
    </tbody>
  </table>
  <p class="hint" v-if="winCoord.summary">
    合计 {{ winCoord.summary.total }} · 有效 {{ winCoord.summary.valid }} · 无效 {{ winCoord.summary.invalid }} · 冲突 {{ winCoord.summary.conflicts }}
  </p>
</div>
`;
