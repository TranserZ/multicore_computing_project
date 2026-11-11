# multicore_computing_project
1. Goal
    1. 직접 성능–전력–온도 평가 흐름(flow)을 구축하고, 그걸로 실제 아키텍처 아이디어를 연구하는 것.
    2. 즉, 평가 tool을 만들고 새로운 아키텍처 아이디어를 만들어보자. 
2. **구성: 2개 문제, 총 25점**
    1. **Problem 1 (10점)**: gem5 + McPAT + HotSpot을 연결하고 멀티코어 out-of-order 실행을 시뮬레이션
    2. **Problem 2 (15점)**: 기존 아키텍처 기법 하나를 구현하고, 그보다 개선된 방법을 제안
        1. 현존하는 아키텍처 하나를 가져오고 분석
        2. 이후 이거보다 더 나은 아키텍처를 만들어보자. 
3. **팀 구성**: 
    1. 팀당 2~3명
4. **제출물**: 
    1. `팀번호_이름1_이름2_...이름N.zip`
        1. 보고서: **6페이지 이하**, PDF, iCampus 제출
            1. 대부분의 채점은 보고서를 기반으로 진행
        2. 코드
            1. Problem 1 → 툴들을 연결하는 glue code
            2. Problem 2 → 수정한 코드를 diff 파일로
5. **마감**: 학기 말 (~12/18 23:59)

---

### Project 1

1. 어떤 것을 만들어야 하는가?
    1. 수업에서 배운 **3단계 시뮬레이션 파이프라인**을 구현.
2. 각 단계
    1. 첫 번째 단계 : Timing Simulator (gem5, GPGPU-Sim 등)
    2. 두 번째 단계 : Power model (CACTI, McPAT 등)
    3. 세 번째 단계:  Thermal model (HotSpot 등)
3. 실제로 구현할 부분은??
    1. 세 툴은 이미 있는 도구들이고, 과제의 핵심은 툴과 툴 사이를 잇는 glue code이다. 
    2. 예를 들면 다음 흐름으로 진행된다
        1. gem5 → McPAT: 
            1. gem5의 `stats.txt`와 `config.json`을 파싱해서 McPAT이 허용하는 입력 format인 XML로 변환. 
        2. McPAT → HotSpot: 
            1. McPAT 출력에서 블록별 전력을 뽑아 HotSpot이 허용하는 입력 format인 `.ptrace` 형식으로 만들고, 그 블록 이름과 맞는 floorplan(`.flp`)을 준비한다. 
4. 3단계 시뮬레이션 파이프라인으로 진행해야 하는 실험 (크게 두 가지)
    1. Core 수
        1. 1, 4, 8 코어로 바꾸어가며 실험
        2. 나머지는 고정
        3. 답해야 하는 질문
            1. TLP(Thread-Level Parallelism)이 속도·전력·온도 측면에서 얼마나 이득인가?
    2. Superscalar width
        1. 2, 4, 8-wide (fetch/issue/commit 함께)로 바꾸어가며 실험
            1. fetch, issue, commit width를 한 번에 같이 바꿔야 한다. 하나만 넓히면 다른 단계가 병목이 됨!
            2. 실제로는 ROB, IQ, LSQ 크기나 FU 개수도 함께 키울지 정해야 하고, 왜 그렇게 했는지 타당한 이유가 있어야 할 것으로 보임.
        2. 나머지는 고정
            1. 코어 수를 뭘로 고정할까는 추후 고민해봐야 할 문제
        3. 답해야 하는 질문
            1. ILP(Instruction-Level Parallelism)은 얼마나 이득이고, 그 비용은 얼마인가?
    3. 이 실험의 의미는?
        1. A는 코어를 늘리는 방향(scale-out), B는 코어 하나를 넓히는 방향(scale-up)
            1. 이 두 방향의 비용 대비 효과를 비교하는 게 핵심!!
    4. 사용하는 Workload = **PARSEC 3.0**, simsmall 입력, ROI만
        1. PARSEC = multicore의 성능을 제기 위한 workload
            1. 이 안에 4가지 input size, 12개의 program이 있음 (이 중 4가지 program)
                1. blackscholes
                    1. 옵션 가격 계산. 
                    2. 계산 위주이고 스레드끼리 거의 독립적이라 코어를 늘리면 잘 확장될 가능성이 큰 workload
                2. canneal:
                    1. 시뮬레이티드 어닐링. 
                    2. 메모리를 무작위로 접근해서 캐시 미스가 많은 memory-bound workload
                3. freqmine
                    1. 빈발 패턴 마이닝(FP-growth). 
                    2. 데이터 구조 순회가 많고, 병렬성은 중간 정도
                4. streamcluster
                    1. 온라인 클러스터링. 
                    2. 메모리 대역폭을 많이 쓰고 barrier 동기화가 잦다. 
            2. simsmall 입력으로 실험해야 함
            3. ROI only
                1. 초기화나 입출력을 빼고 실제 병렬 계산 구간(Region of Interest)만 측정하라는 뜻
                2. PARSEC의 ROI 시작/끝 hook(gem5에서는 m5 ops)에서 통계를 reset/dump해야 하고, 보통 ROI 전까지는 빠른 CPU 모델로 진행하다가 ROI에서 O3 CPU로 바꿔 돌리는 방식을 사용함.
    5. 측정 지표(metrics)
        1. 각 configuration에 대해서 workload별로 기록
            1. **Normalized speedup**
                1. 기준보다 몇 배 빨라졌는가
                    1. 같은 프로그램을 여러 구성으로 돌린 뒤, 가장 작은 구성을 기준(1.0)으로 정하고 나머지가 그보다 몇 배 빠른지 계산한 값
                2. e.g.
                    
                    !image.png
                    
            2. **평균 전력 (W)**
                1. dynamic + static, 칩 전체 기준. McPAT 출력에서 가져옵니다.
            3. **Steady-state 온도 (°C)**
                1. HotSpot의 정상 상태 온도. 평균 전력을 넣고 열평형에 도달했을 때의 값입니다.
5. 무엇을 분석해야 하는 것인가?
    1. core & pipeline width가 바뀌는 상황에서 4가지 application에 대해 아래 값을 뽑아오고 분석하면 됨
        1. speed
        2. power
        3. temperature
6. Grading
    1. 모든 configuration, metric에 대해서 알맞는 결과와 표, 그림 등을 가져왔는가?
    2. 잘 분석했는가?
    3. 코드는 제출했는가?
        1. 코드 없으면 0점

---

### Project 2

1. 기존 아키텍처 기법 하나를 골라서, 한계를 찾고, 더 나은 방법을 제안하는 과제
    1. Step 1: 관심 있는 기법 고르기
        1. 꼭 수업에 없어도 top-tier 논문의 idea 사용 가능
        2. e.g. 
            1. 분기 예측기: TAGE, perceptron predictor
            2. 프리페처: stride prefetcher, stream prefetcher
            3. 캐시 교체 정책: RRIP
            4. Issue queue / wakeup 최적화
            5. 메모리 의존성 예측
            6. 전력 관리: DVFS, power-gating 정책
            7. 등등 …
    2. Step 2: 한계를 짚고 새 방법 제안하기
        1. 고른 기법이 어떤 상황에서 잘 안 되는지 찾고, 그걸 해결하는 아이디어를 낸다.
    3. Step 3: gem5에 구현하기
        1. 원래 기법과 제안 기법을 모두 gem5에 넣는다. 제출은 diff 파일로 한다.
    4. Step 4: Problem 1 flow로 평가하기
        1. Problem 1에서 만든 파이프라인으로 성능, 전력, 온도를 모두 측정
    5. **!!RULE!!**
        1. 공평성을 위해서 아래 세 가지를 같은 구성, 같은 워크로드에서 비교해야 한다.
            1. Baseline (기법 없음, 또는 gem5 기본 설정)
            2. 원래 기법
            3. 내가 개선한 기법
        2. 조건이 다르면 차이가 기법 때문인지 설정 때문인지 구분할 수 없으니, **바꾸는 건 기법 하나뿐이어야 함.**
2. Evaluation
    1. 아이디어를 명확하게 설명할 것
        1. 기존 기법의 한계가 무엇이었는가? 왜 나의 디자인이 이 한계를 개선하는가?
    2. "무엇이 문제였고 → 그래서 이렇게 바꿨다"가 분명히 드러나야 함
    3. Tradeoff를 분명히 밝히면서 비교할 것
        1. 성능 이득 vs. 전력 오버헤드 vs. 온도
            1. 무엇을 얼마만큼 포기하고 무엇을 얼마만큼 얻었는가?
            2. 예: “IPC가 10% 올랐지만 전력은 5% 늘었다”
    4. 성능만 좋아졌다고 쓰는 게 아니라, 그 대가로 무엇을 지불했는지까지 보여 줘야 한다.
        1. 예를 들어 예측기 테이블을 키우면 정확도는 오르지만 SRAM 접근 전력과 면적이 늘어나는데, 이런 부분을 숫자로 보여 주어야 함.
    5. 권장하는 사용 figure
        1. 워크로드별로 {baseline, 원래 기법, 내 개선안} 세 가지를 비교:
            1. Speedup
            2. 평균 전력
            3. Peak 온도
3. Grading
    1. 배점 = 15점 
        1. 아이디어의 참신성과 타당성 (10점): 
            1. 새로우면서도 실제 하드웨어로 구현 가능한 아이디어여야 함
            2. 얼마나 신기하면서 말이 되는가?
                1. base로 잡은 선행연구를 깊게 이해하고 한계를 clear하게 설명해야 함
                2. 분석한 한계로부터 solution은 무엇인지 idea가 얼마나 타당한지 설명해야 함 
        2. 결과 분석 (5점): 
            1. 왜 그런 결과가 나왔는지 분석 + 설명
                1. perf., power, temperature 모두 다!!
        3. 코드는 제출했는가?
            1. 코드 없으면 0점

---

### Report 와 Submission

1. Report
    1. 분량: problem 1 & 2 합쳐서 6 페이지
    2. 형식: pdf
2. 권장 구성
    1. Problem 1
        1. tool을 어떻게 만들었는지
        2. 과제가 요구한 결과와 분석
    2. Problem 2
        1. 기존 아이디어와 그 한계
        2. 나의 제안
        3. 내가 제안한 design의 결과와 분석
    3. conclusion
        1. 무엇을 배웠는지, AI를 어떻게 썼는지, 시간이 더 있었다면 무엇을 했을지 등
3. AI 사용 규칙
    1. 보고서는 **직접 작성!!!**
    2. 나머지에는 AI를 사용하는 것이 자유로움. 
        1. 단, 어떻게 썼는지 conclusion 영역에 간단히 적어야 함
4. 제출 파일 
    1. 제출 방법
        1. iCampus 과제 제출함
    2. 제출 형식
        1. zip
    3. 파일 이름: `팀번호_이름1_..._이름N.zip`
    4. 구성
        1. 보고서 (PDF)
        2. `problem1` 폴더: 코드 등 (주로 glue code)
        3. `problem2` 폴더: 코드 등 (주로 diff 파일)
5. 마감: 학기 말까지 (~12/18 23:59)
6. 점수
    1. 중간고사 & 기말고사 = 30점
    2. Project 1 = 10점
    3. Project 2 = 15점
        1. 중간고사 & 기말고사의 절반. 
        2. 열심히 해야 할 것!!!
