import { useEffect, useState } from 'react';
import NetInfo from '@react-native-community/netinfo';

export interface NetworkState {
  isConnected: boolean;
  isInternetReachable: boolean | null;
  type: string;
}

export function useNetworkState(): NetworkState {
  const [state, setState] = useState<NetworkState>({
    isConnected: true,
    isInternetReachable: true,
    type: 'unknown',
  });

  useEffect(() => {
    const unsubscribe = NetInfo.addEventListener((s) => {
      setState({
        isConnected: !!s.isConnected,
        isInternetReachable: s.isInternetReachable ?? null,
        type: s.type,
      });
    });
    NetInfo.fetch().then((s) => {
      setState({
        isConnected: !!s.isConnected,
        isInternetReachable: s.isInternetReachable ?? null,
        type: s.type,
      });
    });
    return () => unsubscribe();
  }, []);

  return state;
}
