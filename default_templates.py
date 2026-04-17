"""병원 기본양식 기본값 (루시드 동물병원 실제 양식).
DB에 저장되며 Jinja 변수({{ d.vet_name }}, {{ d.surgery_date }}) 사용 가능."""

DEFAULT_YOUTUBE_URL = "https://youtu.be/tlrMdxlodUA"  # 입원전 주의사항 영상 (루시드)

DEFAULT_HEADER = """
<div style="text-align:center; line-height:1.4;">
  <h1 style="margin:0 0 6px 0; letter-spacing:4px;">{{ doc_title }}</h1>
  <div style="font-size:10pt; color:#444;">24시루시드동물메디컬센터</div>
</div>
"""

DEFAULT_DISCLAIMER = """
<h4>다음 검사 및 처치에 대해 동의나 거절을 표시해주세요.</h4>
<div class="consent-items">
  <p>1. 전염병 예방을 위한 항체가 검사 &nbsp; <span class="cb">☐</span> 거절 &nbsp; <span class="cb">☐</span> 동의</p>
  <p>2. 무증상성 심장병 파악을 위한 심장초음파 또는 ProBNP &nbsp; <span class="cb">☐</span> 거절 &nbsp; <span class="cb">☐</span> 동의</p>
  <p>3. 혈액 응고 장애 검사를 위한 응고계 검사 &nbsp; <span class="cb">☐</span> 거절 &nbsp; <span class="cb">☐</span> 동의</p>
  <p>4. 수술 중 혈압 강하 등 위험성이 발생했을 경우 응급처치 및 심폐소생술 &nbsp; <span class="cb">☐</span> 거절 &nbsp; <span class="cb">☐</span> 동의</p>
  <p>5. 입원 기간 중 심폐소생술 &nbsp; <span class="cb">☐</span> 거절 &nbsp; <span class="cb">☐</span> 동의</p>
</div>

<h4 style="margin-top:14pt;">보호자의 약속</h4>
<ol class="promise-list">
  <li>저희 병원에서 진행된 수술이나 처치에 대해서 반드시 저희 병원으로 문의 및 상담을 하셔야 합니다. 급하지 않은 경우 카카오톡, 급한 경우 24시간 응급 내원 해주세요. 타원 의견은 소견서를 가져오시기 바랍니다. 정확하지 않은 의심은 환자와 의료진 사이에 신뢰를 무너뜨리고 결국 환자에게 피해가 갑니다. &nbsp; <span class="cb">☐</span> 네, 꼭 지키겠습니다. &nbsp; <span class="cb">☐</span> 아니오, 거절합니다.</li>
  <li>주치의가 환자를 책임있게 진료하는 시스템입니다. 그러나 주치의와 시간이 맞지 않아 부득이 다른 수의사의 진료를 보는 경우, 주치의와 전화 예약을 해주시면 주치의가 직접 전화드리겠습니다. &nbsp; <span class="cb">☐</span> 네, 꼭 지키겠습니다. &nbsp; <span class="cb">☐</span> 아니오, 거절합니다.</li>
  <li>우리의 환자는 자신의 증상에 대해서 말을 하지 못하기 때문에 보호자님과의 소통이 중요합니다. 보호자님께서는 지속적으로 주치의에게 환자 상태를 공유해주셔야 합니다. 입원기간에는 면회 후 환자상태를 카카오톡에 남겨주시고, 수술 직후 3일·퇴원 직후 3일간 환자의 상태(식욕·활력·기타증상)를 주치의에게 공유해 주십시오. &nbsp; <span class="cb">☐</span> 네, 꼭 지키겠습니다. &nbsp; <span class="cb">☐</span> 아니오, 거절합니다.</li>
  <li>의견이 있거나 원장님 상담을 원하시면, 카카오톡으로 "원장님 상담 원합니다."라고 요청하시면 원장 상담이 가능합니다. &nbsp; <span class="cb">☐</span> 네, 꼭 지키겠습니다. &nbsp; <span class="cb">☐</span> 아니오, 거절합니다.</li>
  <li>주치의가 정한 입원기간·통원치료일정·투약지시 등을 잘 따라주시고, 환자가 조금이라도 이상하면 지나치지 마시고 즉시 연락해주시기 바랍니다. &nbsp; <span class="cb">☐</span> 네, 꼭 지키겠습니다. &nbsp; <span class="cb">☐</span> 아니오, 거절합니다.</li>
  <li>입원동의서 작성 전 입원전 주의사항 영상을 꼼꼼히 시청하였으며, 영상 내용에 모두 동의하십니까? &nbsp; <span class="cb">☐</span> 네, 시청했으며, 동의합니다.</li>
</ol>
<p class="small">위의 사항을 꼭 지켜주신다면 환자의 건강 회복을 위해 항상 최선을 다할 것이며, 믿음 가는 진료로 감사함을 보답하겠습니다.</p>

<h4 style="margin-top:14pt;">알려드립니다!</h4>
<ol class="notice-list">
  <li>수술 중 핸드폰 사용에 관하여: 수술사진 촬영 및 기록용으로 사용하고 있습니다.</li>
  <li>회복실에서 마취 깨우는 행동: 때리거나 꼬집는 것처럼 보일 수 있으나, 각성 시키기 위해 꼭 필요한 과정입니다. 혀를 빼는 것 역시 혀를 말아서 질식하는 것을 방지하기 위한 것입니다.</li>
</ol>

<p class="legal" style="margin-top:14pt;">
수의사법 제13조의2 및 같은 법 시행규칙 제13조의2에 따라 위와 같이 수의사로부터 수술·입원 및 진료에 관한 설명을 들었으며 진료행위에 동의합니다. 설명 받은 후유증 또는 부작용의 발생, 동물의 소유자 또는 관리자가 준수사항을 지키지 않아 발생하는 문제, 설명드린 불가피한 문제에 대하여 추후 일체의 민·형사상을 포함하여 이의를 제기하지 않을 것을 서명하며, 수술·입원 및 진료와 관련한 수의학적 처리를 담당 수의사에게 위임할 것을 서면으로 동의합니다.
</p>
"""

DEFAULT_FOOTER = """
<p class="sign-date" style="text-align:center; margin-top:18pt;">{{ d.surgery_date }}</p>
<p class="sign-target" style="text-align:right; margin-top:14pt;">
  보호자 또는 의뢰인: ________________________ (인)<br>
  24시루시드동물메디컬센터 귀하
</p>
"""
