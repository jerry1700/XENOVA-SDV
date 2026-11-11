import interactionTheme from './audio/interaction-theme.mp3';

/**
 * 운전자 화면 음악 카드의 곡 (한 곳에서 바꾼다).
 * mp3 를 import 해서 빌드 결과가 /assets/<이름>-<해시>.mp3 가 된다 (휴대폰 중계·VM3 표시 앱은 /assets/ 만 허용).
 * 파일은 2MB 이하, 저작권 문제 없는 음원만 넣는다.
 */
export const MUSIC = {
  title: 'Interaction Theme',
  artist: 'Xenova',
  src: interactionTheme,
};
