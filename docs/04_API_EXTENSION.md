# 자유 대화용 API 확장 지점

현재 구현은 Ollama 로컬 모델을 사용하며 외부 API 연결은 아직 없다. 외부 서비스로의 자동 전환도 없다.

## 현재 경계

`app.py → Agent → LLMProvider → 구조화 계획 → Toolbox → 계산 엔진`

`das_agent/llm.py`의 `LLMProvider` 계약은 다음과 같다.

```python
class LLMProvider(Protocol):
    name: str
    model: str
    def available(self) -> bool: ...
    def plan(self, system: str, user: str, schema: dict) -> dict | None: ...
```

`Agent(toolbox, provider)`에 공급자를 주입한다. `NullProvider`는 모델 없이 정형 요청을 처리하고 `OllamaProvider`는 localhost의 `/api/chat`에 JSON Schema를 전달한다. 계산·파일·엑셀 모듈은 모델 SDK에 의존하지 않는다.

## API를 연결할 때 추가할 것

1. 선택한 서비스의 공급자 클래스를 새 모듈에 구현한다. API 키는 환경변수나 PC의 비밀 저장소에서 읽고 소스·대화 기록·DB에 넣지 않는다.
2. `available()`에서는 최소한의 연결 상태를 확인한다. `plan()`은 `PLAN_SCHEMA`에 맞는 계획을 반환한다. 생성형 응답도 현재의 스키마·수치·필터 검증을 통과해야 도구를 실행할 수 있다.
3. 사이드바에서 사용자가 명시적으로 API 공급자를 선택하도록 한다. 기존 로컬 공급자의 오류를 외부 서비스 자동 전환으로 처리하지 않는다.
4. 자유 대화가 필요하면 읽기 전용 `answer(question, verified_context)` 계약을 별도로 추가한다. 운영 데이터에 관한 숫자는 실행 ID를 가진 도구 결과에서 렌더링하고, 설명에는 사용한 실행을 연결한다.
5. 질문·현재 설정·열 메타데이터·집계 결과 중 어느 항목을 외부로 보낼지 결정한다. 원본 출고 행을 보내지 않더라도 집계·메타데이터 전송은 외부 전송에 해당한다.
6. 응답 제한 시간, 호출 횟수·비용 한도, 오류 안내를 추가한다. 계산 도구의 설정 검증·최대 시나리오 수 제한은 유지한다.

## 검증 기준

- 같은 요청·계획이면 Ollama와 API 공급자 모두 동일한 계산 도구 결과를 만든다.
- 공급자가 추가한 명시되지 않은 필터가 분석 범위를 바꾸지 않는다.
- 잘못된 인수·미등록 데이터셋·계산 실패에서 성공이나 수치를 꾸며내지 않는다.
- API 서버 장애·키 미설정이어도 업로드·계산·엑셀 기능은 동작한다.
- 기존 `tests/test_agent.py`의 공급자 주입 테스트와 회귀 테스트를 통과한다.

API 공급자·모델·요금과 데이터 전송 범위는 다음 구현 단계에서 선택한다. 이 문서는 API 연결 완료를 의미하지 않는다.
