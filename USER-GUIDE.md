# คู่มือ Piyaphan Artifacts

## ลิงก์

- Dashboard: https://artifacts.piyaphan-p.tech
- Google login: https://artifacts.piyaphan-p.tech/auth/login
- MCP (Streamable HTTP): https://mcp.piyaphan-p.tech/mcp
- OAuth metadata: https://auth.piyaphan-p.tech/.well-known/oauth-authorization-server
- Hello World ที่ deploy ผ่าน MCP: https://hello-world.apps.piyaphan-p.tech
- ตัวอย่าง static: https://demo.apps.piyaphan-p.tech
- ตัวอย่าง Node.js: https://node-demo.apps.piyaphan-p.tech
- Hermes: https://hermes.piyaphan-p.tech

แอปตัวอย่างเป็น private ต้องล็อกอินด้วย Google บัญชีที่ได้รับสิทธิ์ เจ้าของคือ piyaphan.po@gmail.com

## Whitelist และสิทธิ์

1. ล็อกอิน Dashboard แล้วเปิด People & access
2. กรอก Google email → Add to whitelist (ไม่มีการส่งอีเมลอัตโนมัติ)
3. ไป Projects → เลือกแอป → Project settings → Project membership
4. ใส่อีเมลเดียวกัน เลือก Viewer (ดู), Deployer (ดูและ deploy), หรือ Maintainer (รวมแก้ environment/visibility)
5. Disable ในหน้า People ระงับบัญชี; Remove membership ถอนสิทธิ์เฉพาะแอป

บัญชีใน whitelist สร้าง project ของตัวเองได้ อีเมลจาก Google/OAuth token เป็นเจ้าของอัตโนมัติ เฉพาะอีเมลเจ้าของเท่านั้นที่ deploy ทับ, rollback หรือแก้ environment/visibility ได้ แม้ platform owner ก็เขียนทับของคนอื่นไม่ได้ Membership ให้สิทธิ์ดูแอป ไม่ได้ให้เขียนทับข้ามเจ้าของ

## Deploy จาก Dashboard

Projects → New project → ตั้งชื่อและ URL name → เลือก Static หรือ Node.js → Create project → Deploy release → เลือก ZIP → Upload & deploy

Static ต้องมี index.html อยู่ชั้นบนสุดของ ZIP ไม่ครอบด้วยโฟลเดอร์ dist Node ต้องมี package.json และ npm start ที่ฟัง 0.0.0.0:3000 พร้อมตอบ / ด้วย 2xx/3xx ห้ามใส่ .env, .git, node_modules ลง ZIP ขนาดสูงสุด 50 MB

ดูผลในรายการ releases เลือก Logs เพื่อดูปัญหา หรือ Rollback เพื่อ deploy artifact เดิมใหม่ โดยใช้ environment ปัจจุบันและไม่ย้อนฐานข้อมูลภายนอก

## เชื่อม MCP กับ harness

ตั้ง remote MCP แบบ Streamable HTTP ที่ https://mcp.piyaphan-p.tech/mcp เปิด OAuth และขอ scopes ทั้ง `artifacts:read artifacts:write deployments:write` → Login Google → Allow access ไม่ต้องแจก Google client secret ให้ผู้ใช้

สำหรับ OpenCode 1.x ที่ติดตั้งบนเครื่องนี้ เพิ่มรายการนี้ลง opencode.json ของ project โดยรวมกับ config เดิม:

```json
{
  "mcp": {
    "artifacts": {
      "type": "remote",
      "url": "https://mcp.piyaphan-p.tech/mcp",
      "enabled": true,
      "oauth": {"scope": "artifacts:read artifacts:write deployments:write"}
    }
  }
}
```

```sh
opencode mcp auth artifacts
opencode mcp list
```

OpenCode 2.x ใช้ `mcp.servers.artifacts` และไม่ใช้ `enabled`:

```json
{
  "mcp": {
    "servers": {
      "artifacts": {
        "type": "remote",
        "url": "https://mcp.piyaphan-p.tech/mcp",
        "oauth": {"scope": "artifacts:read artifacts:write deployments:write"}
      }
    }
  }
}
```

อ้างอิง OpenCode: https://opencode.ai/docs/mcp-servers/

ตัวอย่างสั่ง agent:

> ใช้ artifacts MCP deploy ไฟล์เว็บใน ./dist ไปยัง project hello-world แพ็ก ZIP ให้ index.html อยู่ชั้นบนสุด ตรวจ SHA256 แล้วรอจน deployment healthy จากนั้นส่ง URL ให้ฉัน รักษา visibility เดิมไว้

ขั้นตอนที่ agent ใช้:

1. projects_list → แสดงเฉพาะ project ของอีเมลที่ล็อกอินและเคย deploy แล้ว; ใช้ projects_create เพื่อสร้าง project ใหม่ของตัวเอง (Dashboard แสดง draft ของตัวเองด้วย)
2. artifacts_create_upload(project_id) → รับ upload URL และ header ใช้ครั้งเดียว
3. ส่ง ZIP ด้วย HTTP PUT ไป URL พร้อม header ที่ได้รับ (harness ต้องส่งไฟล์ผ่าน HTTP ได้)
4. artifacts_finalize(artifact_id, sha256) → ตรวจความครบถ้วน
5. deployments_create(project_id, artifact_id, idempotency_key) → เข้าคิว deploy
6. deployments_get(deployment_id) → รอ healthy/failed
7. deployments_logs → ดู log; deployments_rollback → ย้อน artifact เมื่อต้องการ

รองรับ callback แบบ localhost/127.0.0.1 ที่มี port สำหรับ harness บนเครื่อง สำหรับบริการ hosted ที่ใช้ callback HTTPS ต้องเพิ่ม callback ที่แน่นอนใน allowlist ของ server ก่อน ปัจจุบันอนุญาต callback ของ claude.ai และ claude.com ไว้แล้ว แต่ยังไม่ได้ทดสอบ client เหล่านั้นจริง

## ผลตรวจระบบ

ตรวจผ่าน Google login บัญชีเจ้าของ, browser OAuth consent และ PKCE, MCP initialize/list/call, อัปโหลด Hello World และ deploy ผ่าน MCP จริง, Static/Node deployment, HTTPS/private authorization, rollback, failed deployment รักษา release เดิม, และ network isolation ทดสอบอัตโนมัติ 22 ข้อผ่าน

ข้อจำกัด: build Node ไม่ได้รับ runtime secrets; ข้อมูลแอปถาวรให้ต่อ Supabase หรือบริการภายนอก; backup ที่สร้างไว้ยังอยู่บน VPS ไม่ได้ตั้งเวลาหรือสำเนานอกเครื่อง

## เมื่อ connector เคยได้สิทธิ์อ่านอย่างเดียว

หลังอัปเดต metadata ให้ disconnect แล้ว reconnect เพื่อให้เกิด consent ใหม่ที่มี artifacts:read artifacts:write deployments:write เลือกบัญชี Google ที่ต้องการใช้งานจริง การพิมพ์อีเมลใน prompt ไม่ได้เปลี่ยน identity ของ token ระบบไม่เพิ่มสิทธิ์ให้ token เก่าโดยอัตโนมัติ


Admin permissions: `artifacts:read` allows reading own projects/logs; `artifacts:write` allows creating and configuring own projects; `deployments:write` allows upload, deploy and rollback of own projects. Admins select these under People & access. Revocations apply immediately to existing tokens; new OAuth scopes require reconnection/consent. The admin account is piyaphan.po@gmail.com. The company account piyaphan.p@infinitaskt.com is a member; its artifact permissions remain configurable by the admin. OAuth identity cannot be changed by writing an email in a prompt; sign in with the intended Google account.


## Admin overview: apps, logs และ usage

บัญชี Admin (piyaphan.po@gmail.com) เปิดเมนู **Admin overview** เพื่อดูทุก project แม้เป็นของบัญชีอื่น การดูข้อมูลรวมไม่เปลี่ยนเจ้าของหรืออนุญาตให้ deploy ทับข้ามบัญชี

- เลือกช่วง Last 24 hours / 7 days / 30 days และเลือกแอป
- Apps: เจ้าของ, ผู้ deploy release ล่าสุด, requests, response body bytes, HTTP 4xx/5xx, response time เฉลี่ย, CPU/RAM ของ runtime container ล่าสุด และขนาด ZIP ที่จัดเก็บ
- People: จำนวน Google login, MCP tool calls, deployments, app requests และ bandwidth ของบัญชีที่ระบุตัวตนได้
- Deployment history: ดูย้อนหลังทุก release, กรอง project/email, เปลี่ยนหน้า และกด View logs (ค่า secrets ถูกปิดบัง)

Traffic เริ่มนับเมื่อเปิดฟีเจอร์ ไม่ได้สร้างข้อมูลย้อนหลัง เก็บ aggregate รายชั่วโมง 30 วันและเก็บข้อมูลกันนับซ้ำ 1 วัน ข้อมูล refresh ราว 15–30 วินาที CPU 100% หมายถึงหนึ่ง core; RAM/CPU เป็นค่า runtime container ปัจจุบัน ไม่รวม build/gateway เว็บ public ที่ไม่มี app session เป็น anonymous ไม่สามารถรู้ว่าเป็นใครหรือคำนวณ unique visitors ทั้งหมดได้ Requests รวม assets และนับหลัง request สิ้นสุด จึงไม่ใช่ pageviews และ WebSocket จะนับตอนปิด connection

ระบบไม่เก็บ IP, URL/query strings, cookies หรือ tokens ใน traffic analytics Requests ที่ถูกปฏิเสธก่อนถึง app gateway และ internal health probes ไม่รวมในกราฟ การเก็บแบบ Docker log polling อาจขาดช่วงหาก log หมุนทิ้งระหว่าง worker หยุด จึงควรดูเวลาล่าสุดของ collector คู่กับกราฟ
